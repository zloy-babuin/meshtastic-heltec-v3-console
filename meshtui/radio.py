"""Тонкая обёртка над библиотекой meshtastic: подключение, события и команды."""

from __future__ import annotations

import base64
import threading
import time
from typing import Any, Callable

import meshtastic.util
from meshtastic.mesh_interface import MeshInterface
from meshtastic.protobuf import channel_pb2, config_pb2, mesh_pb2, portnums_pb2
from meshtastic.serial_interface import SerialInterface
from meshtastic.tcp_interface import TCPInterface
from pubsub import pub

BROADCAST = "^all"
MAX_TEXT_BYTES = mesh_pb2.Constants.DATA_PAYLOAD_LEN

# Секции, которые Node.writeConfig() умеет записывать на устройство.
LOCAL_SECTIONS = [
    "device", "position", "power", "network", "display", "lora", "bluetooth", "security",
]
MODULE_SECTIONS = [
    "mqtt", "serial", "external_notification", "store_forward", "range_test",
    "telemetry", "canned_message", "audio", "remote_hardware", "neighbor_info",
    "ambient_lighting", "detection_sensor", "paxcounter", "traffic_management",
]

EventHandler = Callable[[str, dict[str, Any]], None]


def find_ports() -> list[str]:
    """Порты, на которых вероятно висит Meshtastic-устройство."""
    return list(meshtastic.util.findPorts(True))


class Radio:
    """Держит одно подключение к ноде и пересылает события в on_event.

    on_event вызывается из потоков библиотеки, поэтому получатель сам
    отвечает за передачу событий в свой поток (в TUI — call_from_thread).
    События: connected, disconnected, packet, node.
    """

    def __init__(self, on_event: EventHandler) -> None:
        self.on_event = on_event
        self.iface: MeshInterface | None = None
        self.target = ""
        self._lock = threading.Lock()
        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        pub.subscribe(self._on_node, "meshtastic.node.updated")

    # --- подключение ---------------------------------------------------------

    def connect(self, port: str | None = None, host: str | None = None) -> None:
        """Блокирующее подключение; бросает исключение при неудаче."""
        self.close()
        try:
            if host:
                self.target = host
                iface: MeshInterface = TCPInterface(hostname=host)
            else:
                if not port:
                    ports = find_ports()
                    if not ports:
                        raise ConnectionError("устройство Meshtastic не найдено")
                    port = ports[0]
                self.target = port
                iface = SerialInterface(devPath=port)
        except SystemExit as e:  # библиотека местами зовёт sys.exit()
            raise ConnectionError(str(e) or "ошибка подключения") from None
        with self._lock:
            self.iface = iface

    def close(self) -> None:
        with self._lock:
            iface, self.iface = self.iface, None
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass

    @property
    def connected(self) -> bool:
        return self.iface is not None and self.iface.isConnected.is_set()

    @property
    def port_closed(self) -> bool:
        """Порт закрыт библиотекой (кабель выдернут), а не просто нода перезагружается.

        При перезагрузке ноды USB-мост остаётся на месте и порт открыт; при
        отключении кабеля поток чтения падает и библиотека закрывает порт,
        обнуляя stream. У TCP stream всегда None, поэтому проверяем только serial.
        """
        iface = self.iface
        if iface is None:
            return True
        return isinstance(iface, SerialInterface) and (
            iface.stream is None or not iface.stream.is_open
        )

    # --- события библиотеки ---------------------------------------------------

    def _mine(self, interface: Any) -> bool:
        return interface is not None and interface is self.iface

    def _on_receive(self, packet: dict, interface: Any = None) -> None:
        if self._mine(interface):
            self.on_event("packet", {"packet": packet})

    def _on_established(self, interface: Any = None) -> None:
        if self._mine(interface):
            self.on_event("connected", {})

    def _on_lost(self, interface: Any = None) -> None:
        if self._mine(interface):
            self.on_event("disconnected", {})

    def _on_node(self, node: dict, interface: Any = None) -> None:
        if self._mine(interface):
            self.on_event("node", {"node": node})

    # --- чтение состояния -----------------------------------------------------

    def _require(self) -> MeshInterface:
        if self.iface is None:
            raise ConnectionError("нет подключения")
        return self.iface

    @property
    def my_num(self) -> int | None:
        iface = self.iface
        if iface is None or iface.myInfo is None:
            return None
        return iface.myInfo.my_node_num

    def my_node(self) -> dict:
        iface = self.iface
        return (iface.getMyNodeInfo() if iface else None) or {}

    def nodes(self) -> list[dict]:
        iface = self.iface
        if iface is None or iface.nodesByNum is None:
            return []
        return list(iface.nodesByNum.values())

    def node_by_num(self, num: int | None) -> dict | None:
        iface = self.iface
        if iface is None or iface.nodesByNum is None or num is None:
            return None
        return iface.nodesByNum.get(num)

    def firmware(self) -> str:
        iface = self.iface
        meta = getattr(iface, "metadata", None) if iface else None
        return meta.firmware_version if meta else "?"

    def local_config(self):
        return self._require().localNode.localConfig

    def module_config(self):
        return self._require().localNode.moduleConfig

    def channels(self) -> list[tuple[int, str, str]]:
        """Активные каналы: (индекс, имя, роль)."""
        iface = self.iface
        chans = iface.localNode.channels if iface else None
        if not chans:
            return []
        preset = self.preset_name()
        out = []
        for ch in chans:
            if ch.role == channel_pb2.Channel.Role.DISABLED:
                continue
            name = ch.settings.name or preset
            role = channel_pb2.Channel.Role.Name(ch.role)
            out.append((ch.index, name, role))
        return out

    def preset_name(self) -> str:
        """Имя пресета как его показывает приложение: LONG_FAST -> LongFast."""
        try:
            lora = self.local_config().lora
        except ConnectionError:
            return "?"
        name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        return "".join(part.capitalize() for part in name.split("_"))

    # --- команды --------------------------------------------------------------

    def send_text(
        self,
        text: str,
        dest: str = BROADCAST,
        channel: int = 0,
        on_ack: Callable[[str, int | None], None] | None = None,
    ) -> dict:
        """Отправляет текст; on_ack(errorReason, from_num) придёт при ACK/NAK."""
        iface = self._require()

        def _cb(p: dict) -> None:
            routing = p.get("decoded", {}).get("routing", {})
            if on_ack:
                on_ack(routing.get("errorReason", "NONE"), p.get("from"))

        return iface.sendData(
            text.encode("utf-8"),
            dest,
            portNum=portnums_pb2.PortNum.TEXT_MESSAGE_APP,
            wantAck=True,
            onResponse=_cb if on_ack else None,
            onResponseAckPermitted=True,
            channelIndex=channel,
        )

    def set_time(self, ts: int | None = None) -> None:
        self._require().localNode.setTime(ts or int(time.time()))

    def set_owner(self, long_name: str, short_name: str) -> None:
        self._require().localNode.setOwner(long_name=long_name, short_name=short_name)

    def write_section(self, section: str) -> None:
        if section not in LOCAL_SECTIONS and section not in MODULE_SECTIONS:
            raise ValueError(f"неизвестная секция {section}")
        self._require().localNode.writeConfig(section)

    def reboot(self, secs: int = 5) -> None:
        self._require().localNode.reboot(secs)

    def shutdown(self, secs: int = 5) -> None:
        self._require().localNode.shutdown(secs)


def short_key(key: str | bytes | None) -> str:
    if not key:
        return "—"
    if isinstance(key, bytes):
        key = base64.b64encode(key).decode()
    return key if len(key) <= 20 else key[:10] + "…" + key[-6:]
