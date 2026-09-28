"""meshtui — консольный клиент Meshtastic: чат, узлы, эфир, настройки."""

from __future__ import annotations

import argparse
import logging
import time
from typing import Any, Callable

from meshtastic.protobuf import config_pb2
from rich.markup import escape
from rich.table import Table
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Header, Input, Label, ListItem, ListView,
    RichLog, Select, Static, TabbedContent, TabPane,
)

from . import __version__
from .config_form import ConfigForm
from .radio import (
    BROADCAST, LOCAL_SECTIONS, MAX_TEXT_BYTES, MODULE_SECTIONS, Radio, find_ports, short_key,
)

# Сколько ждём, пока библиотека сама поднимет связь после перезагрузки ноды,
# прежде чем переоткрыть порт с нуля.
RECONNECT_GRACE = 30


# --- форматирование ------------------------------------------------------------

def clock(ts: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts or time.time()))


def ago(ts: float | None) -> str:
    if not ts:
        return "—"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d} с"
    if d < 3600:
        return f"{d // 60} мин"
    if d < 86400:
        return f"{d // 3600} ч"
    return f"{d // 86400} д"


def duration(secs: int | None) -> str:
    if secs is None:
        return "—"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}д {h}ч {m}м" if d else f"{h}ч {m}м"


def battery(metrics: dict) -> str:
    level, volt = metrics.get("batteryLevel"), metrics.get("voltage")
    if level is None:
        return "—"
    text = "🔌 USB" if level > 100 else f"🔋 {level}%"
    return f"{text} {volt:.2f} В" if volt else text


def summarize(p: dict) -> tuple[str, str]:
    """(тип пакета, краткое содержимое) для журнала эфира."""
    d = p.get("decoded")
    if d is None:
        return "ENCRYPTED", "зашифрован ключом чужого канала"
    port = d.get("portnum", "?")
    if port == "TEXT_MESSAGE_APP":
        return "TEXT", d.get("text", "")
    if port == "POSITION_APP":
        pos = d.get("position", {})
        lat, lon = pos.get("latitude"), pos.get("longitude")
        if lat is None or lon is None:
            return "POSITION", "без координат"
        alt = pos.get("altitude")
        return "POSITION", f"{lat:.5f}, {lon:.5f}" + (f", {alt} м" if alt is not None else "")
    if port == "NODEINFO_APP":
        u = d.get("user", {})
        return "NODEINFO", f"{u.get('longName', '?')} ({u.get('shortName', '?')}) {u.get('hwModel', '')}"
    if port == "TELEMETRY_APP":
        t = d.get("telemetry", {})
        parts = []
        if dm := t.get("deviceMetrics"):
            parts.append(battery(dm))
            if (cu := dm.get("channelUtilization")) is not None:
                parts.append(f"эфир {cu:.1f}%")
        if em := t.get("environmentMetrics"):
            if (v := em.get("temperature")) is not None:
                parts.append(f"{v:.1f} °C")
            if (v := em.get("relativeHumidity")) is not None:
                parts.append(f"{v:.0f}% влажн.")
            if (v := em.get("barometricPressure")) is not None:
                parts.append(f"{v:.0f} гПа")
        if lm := t.get("localStats"):
            parts.append(f"онлайн {lm.get('numOnlineNodes', 0)}/{lm.get('numTotalNodes', 0)} узлов")
            parts.append(f"пакетов TX {lm.get('numPacketsTx', 0)} / RX {lm.get('numPacketsRx', 0)}")
        if not parts:
            parts = [k for k in t if k != "time"]
        return "TELEMETRY", ", ".join(parts)
    if port == "ROUTING_APP":
        err = d.get("routing", {}).get("errorReason", "NONE")
        return "ROUTING", "ACK" if err == "NONE" else err
    if port == "NEIGHBORINFO_APP":
        n = len(d.get("neighborinfo", {}).get("neighbors", []))
        return "NEIGHBORS", f"соседей: {n}"
    return port.removesuffix("_APP"), ""


def signal(p: dict) -> str:
    parts = []
    if (snr := p.get("rxSnr")) is not None:
        parts.append(f"SNR {snr:.1f}")
    if (rssi := p.get("rxRssi")) is not None:
        parts.append(f"RSSI {rssi}")
    start, limit = p.get("hopStart"), p.get("hopLimit")
    if start is not None and limit is not None:
        parts.append(f"хопов {start - limit}")
    return " · ".join(parts)


# --- экраны --------------------------------------------------------------------

class Confirm(ModalScreen[bool]):
    """Да/нет перед действиями, которые меняют устройство."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Отмена")]

    def __init__(self, title: str, body: str) -> None:
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self._body)
            with Horizontal(id="dialog-buttons"):
                yield Button("Да", variant="warning", id="yes")
                yield Button("Отмена", id="no")

    @on(Button.Pressed)
    def _answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# --- приложение ----------------------------------------------------------------

class MeshTUI(App):
    TITLE = "meshtui"
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    TabbedContent, ContentSwitcher, TabPane { height: 1fr; }
    TabPane { padding: 0; }
    #chat, #packetlog { height: 1fr; border: round $primary-darken-2; }
    #compose { height: auto; }
    #dest { width: 38; }
    #msg { width: 1fr; }
    #counter { width: 11; padding: 1 1 0 1; color: $text-muted; }
    #counter.over { color: $error; text-style: bold; }
    #nodetable { height: 1fr; }
    #device-body { padding: 0 1; }
    #devinfo { height: auto; margin-bottom: 1; }
    .form-row { height: auto; margin-bottom: 1; }
    .form-row Label { width: 16; padding-top: 1; }
    #long { width: 40; }
    #short { width: 12; }
    .form-row Button { margin-left: 1; }
    #sections { width: 30; border: round $primary-darken-2; }
    #settings-body { width: 1fr; padding-left: 1; }
    #sec-title { height: 1; text-style: bold; }
    #form { height: 1fr; }
    #cfg-buttons { height: auto; }
    #cfg-buttons Button { margin-right: 1; }
    .group { color: $text-muted; text-style: italic; }
    Confirm { align: center middle; }
    #dialog { width: 80; height: auto; max-height: 85%; border: thick $warning; background: $surface; padding: 1 2; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog-body { height: auto; max-height: 20; }
    #dialog-buttons { height: auto; margin-top: 1; }
    #dialog-buttons Button { margin-right: 2; }
    """
    BINDINGS = [
        Binding("ctrl+q", "quit", "Выход", priority=True),
        Binding("f1", "show('messages')", "Чат"),
        Binding("f2", "show('nodes')", "Узлы"),
        Binding("f3", "show('packets')", "Эфир"),
        Binding("f4", "show('device')", "Устройство"),
        Binding("f5", "show('settings')", "Настройки"),
    ]

    def __init__(self, port: str | None, host: str | None, sync_time: bool) -> None:
        super().__init__()
        self.port, self.host, self.sync_time = port, host, sync_time
        self.radio = Radio(self._from_radio)
        self._online = False
        self._lost_at: float | None = None
        self._conn_msg = "Подключение…"
        self._dest_keys: tuple[str, ...] = ()
        self._node_rows: list[tuple] = []
        self._section: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with TabbedContent(initial="messages"):
            with TabPane("Чат \\[F1]", id="messages"):
                yield RichLog(id="chat", wrap=True, markup=True)
                with Horizontal(id="compose"):
                    yield Select([], prompt="Кому", id="dest")
                    yield Input(placeholder="Сообщение, Enter — отправить", id="msg")
                    yield Label(f"0/{MAX_TEXT_BYTES}", id="counter")
            with TabPane("Узлы \\[F2]", id="nodes"):
                yield DataTable(id="nodetable", cursor_type="row", zebra_stripes=True)
            with TabPane("Эфир \\[F3]", id="packets"):
                yield RichLog(id="packetlog", wrap=False, markup=True, max_lines=5000)
            with TabPane("Устройство \\[F4]", id="device"):
                with VerticalScroll(id="device-body"):
                    yield Static(id="devinfo")
                    with Horizontal(classes="form-row"):
                        yield Label("Имя")
                        yield Input(id="long", max_length=39, placeholder="длинное имя")
                        yield Input(id="short", max_length=4, placeholder="кор.")
                        yield Button("Сохранить имя", id="save-owner")
                    with Horizontal(classes="form-row"):
                        yield Button("Синхронизировать время с ПК", id="sync-time", variant="primary")
                        yield Button("Перезагрузить", id="reboot", variant="warning")
                        yield Button("Выключить", id="shutdown", variant="error")
            with TabPane("Настройки \\[F5]", id="settings"):
                with Horizontal():
                    yield ListView(*self._section_items(), id="sections")
                    with Vertical(id="settings-body"):
                        yield Label("Выбери секцию слева и нажми Enter", id="sec-title")
                        yield ConfigForm(id="form")
                        with Horizontal(id="cfg-buttons"):
                            yield Button("Записать на устройство", id="cfg-save", variant="primary")
                            yield Button("Отменить правки", id="cfg-revert")
        yield Footer()

    @staticmethod
    def _section_items() -> list[ListItem]:
        items = [ListItem(Label("— конфигурация —", classes="group"), disabled=True)]
        items += [ListItem(Label(s), id=f"sec-{s}") for s in LOCAL_SECTIONS]
        items.append(ListItem(Label("— модули —", classes="group"), disabled=True))
        items += [ListItem(Label(s), id=f"sec-{s}") for s in MODULE_SECTIONS]
        return items

    def on_mount(self) -> None:
        table = self.query_one("#nodetable", DataTable)
        table.add_columns("", "Имя", "Кор.", "ID", "Модель", "SNR", "Хопы", "Питание", "Слышали")
        self.set_interval(2, self._tick)
        self._update_status()
        self._connect()

    def on_unmount(self) -> None:
        self.radio.close()

    # Куда ставить фокус при переходе на вкладку.
    TAB_FOCUS = {
        "messages": "#msg", "nodes": "#nodetable", "packets": "#packetlog",
        "device": "#long", "settings": "#sections",
    }

    def action_show(self, tab: str) -> None:
        # Сначала снимаем фокус: иначе он перескочит на соседний виджет старой
        # вкладки, и TabbedContent вернёт её обратно.
        self.set_focus(None)
        self.query_one(TabbedContent).active = tab
        self.query_one(self.TAB_FOCUS[tab]).focus()

    # --- связь с радио --------------------------------------------------------

    def _from_radio(self, kind: str, data: dict) -> None:
        """Вызывается из потоков meshtastic; переносит событие в поток UI."""
        handlers: dict[str, Callable[..., None]] = {
            "connected": lambda: self._on_connected(),
            "disconnected": lambda: self._on_disconnected(),
            "packet": lambda: self._on_packet(data["packet"]),
            "node": lambda: None,  # таблица узлов обновляется по таймеру
        }
        try:
            self.call_from_thread(handlers[kind])
        except RuntimeError:
            pass  # приложение уже закрывается

    @work(thread=True, exclusive=True, group="connect")
    def _connect(self) -> None:
        from textual.worker import get_current_worker

        worker = get_current_worker()
        target = self.host or self.port or "автопоиск порта"
        while not worker.is_cancelled:
            self.call_from_thread(self._set_conn_msg, f"Подключение ({target})…")
            try:
                self.radio.connect(self.port, self.host)
            except Exception as e:
                self.call_from_thread(
                    self._set_conn_msg, f"Нет подключения: {e}. Повтор через 3 с"
                )
                time.sleep(3)
                continue
            self.call_from_thread(self._on_connected)
            return

    def _set_conn_msg(self, msg: str) -> None:
        self._conn_msg = msg
        self._update_status()

    def _on_connected(self) -> None:
        self._lost_at = None
        if self._online:
            return
        self._online = True
        me = self.radio.my_node().get("user", {})
        self.sub_title = f"{me.get('longName', '?')} · {self.radio.target}"
        self._log_sys(f"Подключено к {self.radio.target}")
        self.query_one("#long", Input).value = me.get("longName", "")
        self.query_one("#short", Input).value = me.get("shortName", "")
        if self.sync_time:
            self._run(self.radio.set_time, ok="Время ноды синхронизировано с ПК")
        self._refresh_all()

    def _on_disconnected(self) -> None:
        if not self._online:
            return
        self._online = False
        self._lost_at = time.time()
        self._conn_msg = "Связь потеряна (нода перезагружается?) — жду…"
        self._log_sys("Связь с нодой потеряна")
        self._update_status()

    def _tick(self) -> None:
        if (
            self._lost_at is not None
            and not self.radio.connected
            and (self.radio.port_closed or time.time() - self._lost_at > RECONNECT_GRACE)
        ):
            self._lost_at = None
            self._log_sys("Переоткрываю порт")
            self._connect()
        self._refresh_all()

    def _refresh_all(self) -> None:
        self._update_status()
        if not self._online:
            return
        self._refresh_nodes()
        self._refresh_dest()
        if self.query_one(TabbedContent).active == "device":
            self._refresh_device()

    @work(thread=True, group="cmd")
    def _run(self, fn: Callable[..., Any], *args: Any, ok: str | None = None) -> None:
        """Выполняет блокирующую команду радио вне потока UI."""
        try:
            fn(*args)
        except (Exception, SystemExit) as e:
            self.call_from_thread(self.notify, f"Ошибка: {e}", severity="error")
        else:
            if ok:
                self.call_from_thread(self.notify, ok)

    # --- строка статуса и вкладка устройства -----------------------------------

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if not self._online:
            status.update(f"[red]○[/] {escape(self._conn_msg)}")
            return
        node = self.radio.my_node()
        user = node.get("user", {})
        metrics = node.get("deviceMetrics", {})
        lora = self.radio.local_config().lora
        region = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
        parts = [
            f"[green]●[/] [b]{escape(user.get('longName', '?'))}[/] ({escape(user.get('shortName', '?'))}) {user.get('id', '')}",
            user.get("hwModel", "?"),
            f"fw {self.radio.firmware().rsplit('.', 1)[0]}",
            f"{region} · {self.radio.preset_name()} · {lora.tx_power} дБм",
            battery(metrics),
        ]
        if (cu := metrics.get("channelUtilization")) is not None:
            parts.append(f"эфир {cu:.1f}% / TX {metrics.get('airUtilTx', 0):.1f}%")
        parts.append(f"узлов {len(self.radio.nodes())}")
        status.update(" │ ".join(parts))

    def _refresh_device(self) -> None:
        node = self.radio.my_node()
        user = node.get("user", {})
        metrics = node.get("deviceMetrics", {})
        cfg = self.radio.local_config()
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold")
        t.add_column()
        rows = [
            ("Имя", f"{user.get('longName', '?')} ({user.get('shortName', '?')})"),
            ("ID / номер", f"{user.get('id', '?')} / {node.get('num', '?')}"),
            ("Модель", user.get("hwModel", "?")),
            ("Прошивка", self.radio.firmware()),
            ("Роль", config_pb2.Config.DeviceConfig.Role.Name(cfg.device.role)),
            ("Регион / пресет", f"{config_pb2.Config.LoRaConfig.RegionCode.Name(cfg.lora.region)} / {self.radio.preset_name()}"),
            ("Мощность TX", f"{cfg.lora.tx_power} дБм"),
            ("Часовой пояс", cfg.device.tzdef or "UTC"),
            ("Питание", battery(metrics)),
            ("Загрузка эфира", f"{metrics.get('channelUtilization', 0):.1f}% (своя TX {metrics.get('airUtilTx', 0):.1f}%)"),
            ("Аптайм", duration(metrics.get("uptimeSeconds"))),
            ("Время ПК", time.strftime("%d.%m.%Y %H:%M:%S %Z")),
            ("Публичный ключ", short_key(user.get("publicKey"))),
            ("Порт", self.radio.target),
        ]
        for idx, name, role in self.radio.channels():
            rows.append((f"Канал {idx}", f"{name} ({role})"))
        for k, v in rows:
            t.add_row(k, escape(str(v)))
        self.query_one("#devinfo", Static).update(t)

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, event: TabbedContent.TabActivated) -> None:
        if self._online and event.pane.id == "device":
            self._refresh_device()

    @on(Button.Pressed, "#save-owner")
    def _save_owner(self) -> None:
        long_name = self.query_one("#long", Input).value.strip()
        short_name = self.query_one("#short", Input).value.strip()
        if not long_name or not short_name:
            self.notify("Нужны оба имени", severity="error")
            return
        self._run(self.radio.set_owner, long_name, short_name, ok=f"Имя: {long_name} ({short_name})")

    @on(Button.Pressed, "#sync-time")
    def _sync_time(self) -> None:
        self._run(self.radio.set_time, ok=f"Время ноды выставлено: {clock()}")

    @on(Button.Pressed, "#reboot")
    def _reboot_node(self) -> None:
        def done(yes: bool | None) -> None:
            if yes:
                self._run(self.radio.reboot, ok="Нода перезагрузится через 5 с")
        self.push_screen(Confirm("Перезагрузить ноду?", "Время на ноде собьётся; приложение выставит его заново после переподключения."), done)

    @on(Button.Pressed, "#shutdown")
    def _shutdown_node(self) -> None:
        def done(yes: bool | None) -> None:
            if yes:
                self._run(self.radio.shutdown, ok="Нода выключится через 5 с")
        self.push_screen(Confirm("Выключить ноду?", "Включить её обратно можно кнопкой на плате или переподключением питания."), done)

    # --- узлы ------------------------------------------------------------------

    def _refresh_nodes(self) -> None:
        my = self.radio.my_num
        rows = []
        for n in sorted(self.radio.nodes(), key=lambda n: -(n.get("lastHeard") or 0)):
            user = n.get("user", {})
            nid = user.get("id") or f"!{n.get('num', 0):08x}"
            me = n.get("num") == my
            snr, hops = n.get("snr"), n.get("hopsAway")
            rows.append((
                nid,
                "★" if me else "",
                user.get("longName", "?"),
                user.get("shortName", ""),
                nid,
                user.get("hwModel", ""),
                f"{snr:.1f}" if snr is not None else "—",
                "0" if me else (str(hops) if hops is not None else "—"),
                battery(n.get("deviceMetrics", {})),
                "это я" if me else ago(n.get("lastHeard")),
            ))
        if rows == self._node_rows:
            return
        self._node_rows = rows
        table = self.query_one("#nodetable", DataTable)
        current = None
        if table.row_count:
            current = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear()
        for key, *cells in rows:
            table.add_row(*cells, key=key)
        if current is not None and current in {r[0] for r in rows}:
            table.move_cursor(row=table.get_row_index(current))

    @on(DataTable.RowSelected, "#nodetable")
    def _node_selected(self, event: DataTable.RowSelected) -> None:
        key = f"dm:{event.row_key.value}"
        self._refresh_dest()
        if key in self._dest_keys:
            self.query_one("#dest", Select).value = key
            self.action_show("messages")

    # --- чат ---------------------------------------------------------------------

    def _refresh_dest(self) -> None:
        opts: list[tuple[str, str]] = []
        for idx, name, _role in self.radio.channels():
            opts.append((f"# {name}" + (f" (канал {idx})" if idx else ""), f"ch:{idx}"))
        my = self.radio.my_num
        others = [
            n for n in self.radio.nodes()
            if n.get("num") != my and n.get("user", {}).get("id")
        ]
        for n in sorted(others, key=lambda n: n["user"].get("longName", "").lower()):
            u = n["user"]
            opts.append((f"@ {u.get('longName', '?')} ({u.get('shortName', '')})", f"dm:{u['id']}"))
        keys = tuple(v for _, v in opts)
        if keys == self._dest_keys:
            return
        sel = self.query_one("#dest", Select)
        current = sel.value
        sel.set_options(opts)
        if current in keys:
            sel.value = current
        elif opts:
            sel.value = opts[0][1]
        self._dest_keys = keys

    def _node_label(self, num: int | None, fallback: str | None = None) -> str:
        if num is not None and num == self.radio.my_num:
            return "я"
        node = self.radio.node_by_num(num)
        user = (node or {}).get("user", {})
        return user.get("shortName") or user.get("longName") or fallback or (f"!{num:08x}" if num else "?")

    def _channel_name(self, idx: int) -> str:
        for i, name, _ in self.radio.channels():
            if i == idx:
                return name
        return f"канал {idx}"

    def _log_sys(self, text: str) -> None:
        self.query_one("#chat", RichLog).write(f"[dim]{clock()} ── {escape(text)}[/]")

    @on(Input.Changed, "#msg")
    def _count_bytes(self, event: Input.Changed) -> None:
        n = len(event.value.encode("utf-8"))
        counter = self.query_one("#counter", Label)
        counter.update(f"{n}/{MAX_TEXT_BYTES}")
        counter.set_class(n > MAX_TEXT_BYTES, "over")

    @on(Input.Submitted, "#msg")
    def _submit(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        if not self._online:
            self.notify("Нет подключения к ноде", severity="error")
            return
        if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            self.notify(f"Сообщение длиннее {MAX_TEXT_BYTES} байт (кириллица — 2 байта на букву)", severity="error")
            return
        value = self.query_one("#dest", Select).value
        kind, _, arg = (value if isinstance(value, str) else "ch:0").partition(":")
        if kind == "dm":
            dest, channel, label = arg, 0, f"@{self._node_label(self._num_of(arg), arg)}"
        else:
            dest, channel, label = BROADCAST, int(arg), f"#{self._channel_name(int(arg))}"
        event.input.value = ""
        self._send(text, dest, channel, label)

    def _num_of(self, node_id: str) -> int | None:
        try:
            return int(node_id.lstrip("!"), 16)
        except ValueError:
            return None

    @work(thread=True, group="send")
    def _send(self, text: str, dest: str, channel: int, label: str) -> None:
        dest_num = None if dest == BROADCAST else self._num_of(dest)

        def on_ack(err: str, frm: int | None) -> None:
            try:
                self.call_from_thread(self._show_ack, text, label, err, frm, dest_num)
            except RuntimeError:
                pass

        try:
            self.radio.send_text(text, dest, channel, on_ack)
        except (Exception, SystemExit) as e:
            self.call_from_thread(self.notify, f"Не отправлено: {e}", severity="error")
            return
        self.call_from_thread(
            self.query_one("#chat", RichLog).write,
            f"[dim]{clock()}[/] [b cyan]я → {escape(label)}:[/] {escape(text)}",
        )

    def _show_ack(self, text: str, label: str, err: str, frm: int | None, dest_num: int | None) -> None:
        snippet = escape(text if len(text) <= 30 else text[:29] + "…")
        if err != "NONE":
            mark = f"[red]✗ не доставлено ({escape(err)})[/]"
        elif dest_num is None:
            mark = "[green]✓ ушло в сеть[/] [dim](слышали ретрансляцию или приняла нода)[/]"
        elif frm == dest_num:
            mark = f"[green]✓ доставлено[/] {escape(label)}"
        else:
            mark = "[yellow]✓ принято ретранслятором[/] [dim](подтверждения от адресата нет)[/]"
        self.query_one("#chat", RichLog).write(f"[dim]{clock()}[/]   {mark}: [dim]«{snippet}»[/]")

    # --- входящие пакеты --------------------------------------------------------

    def _on_packet(self, p: dict) -> None:
        kind, info = summarize(p)
        frm = self._node_label(p.get("from"), p.get("fromId"))
        to = "все" if p.get("toId") == BROADCAST else self._node_label(p.get("to"), p.get("toId"))
        ts = p.get("rxTime") or time.time()
        sig = signal(p)
        self.query_one("#packetlog", RichLog).write(
            f"[dim]{clock(ts)}[/] [b]{escape(frm):>5}[/] → {escape(to):<5} "
            f"[magenta]{kind:<10}[/] {escape(info)}" + (f"  [dim]{sig}[/]" if sig else "")
        )
        if kind == "TEXT" and p.get("from") != self.radio.my_num:
            if p.get("toId") == BROADCAST:
                where = f"#{self._channel_name(p.get('channel', 0))}"
            else:
                where = "лично"
            self.query_one("#chat", RichLog).write(
                f"[dim]{clock(ts)}[/] [b green]{escape(frm)}[/] [dim]({escape(where)})[/]: {escape(info)}"
                + (f"  [dim]{sig}[/]" if sig else "")
            )
            self.bell()

    # --- настройки ----------------------------------------------------------------

    def _section_msg(self, name: str):
        cfg = self.radio.local_config() if name in LOCAL_SECTIONS else self.radio.module_config()
        return getattr(cfg, name)

    @on(ListView.Selected, "#sections")
    async def _open_section(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        if not item_id.startswith("sec-"):
            return
        if not self._online:
            self.notify("Нет подключения к ноде", severity="error")
            return
        await self._load_section(item_id.removeprefix("sec-"))

    async def _load_section(self, name: str) -> None:
        self._section = name
        kind = "конфигурация" if name in LOCAL_SECTIONS else "модуль"
        self.query_one("#sec-title", Label).update(f"{name} — {kind}")
        await self.query_one("#form", ConfigForm).load(self._section_msg(name))

    @on(Button.Pressed, "#cfg-revert")
    async def _revert(self) -> None:
        if self._section and self._online:
            await self._load_section(self._section)

    @on(Button.Pressed, "#cfg-save")
    def _save_section(self) -> None:
        section = self._section
        if not section or not self._online:
            self.notify("Сначала выбери секцию", severity="warning")
            return
        form = self.query_one("#form", ConfigForm)
        current = self._section_msg(section)
        edited = type(current)()
        edited.CopyFrom(current)
        try:
            changes = form.apply(edited)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        if not changes:
            self.notify("Изменений нет")
            return
        body = "\n".join(
            f"{escape(path)}: {escape(form.describe(path, old))} → [b]{escape(form.describe(path, new))}[/]"
            for path, old, new in changes
        )
        body += "\n\n[dim]После записи нода может перезагрузиться — приложение переподключится само.[/]"

        def done(yes: bool | None) -> None:
            if not yes:
                return
            self._section_msg(section).CopyFrom(edited)
            self._run(self.radio.write_section, section, ok=f"Секция {section} записана")

        self.push_screen(Confirm(f"Записать секцию {section}?", body), done)


def main() -> None:
    parser = argparse.ArgumentParser(prog="meshtui", description="Консольный клиент Meshtastic")
    parser.add_argument("-p", "--port", help="последовательный порт (/dev/ttyUSB0, COM3); по умолчанию — автопоиск")
    parser.add_argument("--host", help="подключиться по TCP/Wi-Fi вместо USB")
    parser.add_argument("--no-time-sync", action="store_true", help="не выставлять время ноды при подключении")
    parser.add_argument("--list-ports", action="store_true", help="показать найденные порты и выйти")
    parser.add_argument("--log", metavar="FILE", help="писать отладочный лог библиотеки в файл")
    parser.add_argument("--version", action="version", version=f"meshtui {__version__}")
    args = parser.parse_args()

    if args.list_ports:
        ports = find_ports()
        print("\n".join(ports) if ports else "Устройства Meshtastic не найдены")
        return

    # Библиотека пишет предупреждения в stderr — это ломает экран TUI.
    root = logging.getLogger()
    if args.log:
        logging.basicConfig(filename=args.log, level=logging.DEBUG,
                            format="%(asctime)s %(threadName)s %(name)s %(levelname)s %(message)s")
    else:
        root.addHandler(logging.NullHandler())

    MeshTUI(args.port, args.host, sync_time=not args.no_time_sync).run()


if __name__ == "__main__":
    main()
