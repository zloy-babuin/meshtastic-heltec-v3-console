"""Форма редактирования секции конфигурации, построенная по protobuf-дескриптору."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from google.protobuf.descriptor import FieldDescriptor as FD
from google.protobuf.message import Message
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Input, Label, Select, Static, Switch

INT_TYPES = {
    FD.TYPE_INT32, FD.TYPE_INT64, FD.TYPE_UINT32, FD.TYPE_UINT64,
    FD.TYPE_SINT32, FD.TYPE_SINT64, FD.TYPE_FIXED32, FD.TYPE_FIXED64,
    FD.TYPE_SFIXED32, FD.TYPE_SFIXED64,
}
UNSIGNED_TYPES = {FD.TYPE_UINT32, FD.TYPE_UINT64, FD.TYPE_FIXED32, FD.TYPE_FIXED64}
FLOAT_TYPES = {FD.TYPE_FLOAT, FD.TYPE_DOUBLE}
SECRET_SUFFIXES = ("psk", "password")


def _is_repeated(field: FD) -> bool:
    rep = getattr(field, "is_repeated", None)
    return rep if rep is not None else field.label == FD.LABEL_REPEATED


@dataclass
class FieldRef:
    path: str          # "ipv4_config.ip"
    field: FD
    editable: bool


def iter_fields(msg: Message, prefix: str = "") -> list[FieldRef]:
    """Плоский список полей; вложенные сообщения раскрываются через точку."""
    out: list[FieldRef] = []
    for f in msg.DESCRIPTOR.fields:
        path = prefix + f.name
        if _is_repeated(f):
            out.append(FieldRef(path, f, False))
        elif f.type == FD.TYPE_MESSAGE:
            out.extend(iter_fields(getattr(msg, f.name), path + "."))
        else:
            out.append(FieldRef(path, f, f.type != FD.TYPE_BYTES))
    return out


def get_path(msg: Message, path: str) -> Any:
    obj: Any = msg
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def set_path(msg: Message, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    obj: Any = msg
    for part in parents:
        obj = getattr(obj, part)
    setattr(obj, last, value)


def _widget_id(path: str) -> str:
    return "cf-" + path.replace(".", "--")


def _readonly_text(value: Any) -> str:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode() or "—"
    try:
        items = list(value)
    except TypeError:
        return str(value)
    return ", ".join(
        base64.b64encode(v).decode() if isinstance(v, bytes) else str(v) for v in items
    ) or "—"


class ConfigForm(VerticalScroll):
    """Поля одной секции. load() строит форму, apply() переносит значения в копию."""

    DEFAULT_CSS = """
    ConfigForm .cf-row { height: 1; margin-bottom: 1; }
    ConfigForm .cf-label { width: 34; }
    ConfigForm .cf-ro { width: 1fr; color: $text-muted; }
    ConfigForm Input, ConfigForm Select { width: 1fr; }
    ConfigForm Switch { border: none; height: 1; padding: 0; }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fields: list[FieldRef] = []

    async def load(self, msg: Message) -> None:
        await self.remove_children()
        self.fields = iter_fields(msg)
        rows = [self._row(ref, get_path(msg, ref.path)) for ref in self.fields]
        if not rows:
            rows = [Static("В этой секции нет полей.")]
        await self.mount_all(rows)
        self.scroll_home(animate=False)

    def _row(self, ref: FieldRef, value: Any) -> Horizontal:
        label = Label(ref.path, classes="cf-label")
        f = ref.field
        wid = _widget_id(ref.path)
        if not ref.editable:
            widget: Any = Static(_readonly_text(value), classes="cf-ro")
        elif f.type == FD.TYPE_BOOL:
            widget = Switch(value=bool(value), id=wid)
        elif f.type == FD.TYPE_ENUM:
            opts = [(v.name, v.number) for v in f.enum_type.values]
            if value not in {n for _, n in opts}:
                opts.append((f"<{value}>", value))
            widget = Select(opts, value=value, allow_blank=False, id=wid, compact=True)
        elif f.type in INT_TYPES:
            widget = Input(str(value), type="integer", id=wid, compact=True)
        elif f.type in FLOAT_TYPES:
            widget = Input(f"{float(value):.7g}", type="number", id=wid, compact=True)
        else:
            widget = Input(
                str(value), id=wid, compact=True,
                password=ref.path.endswith(SECRET_SUFFIXES),
            )
        return Horizontal(label, widget, classes="cf-row")

    def apply(self, target: Message) -> list[tuple[str, Any, Any]]:
        """Записывает значения формы в target; возвращает [(поле, было, стало)].

        Бросает ValueError с понятным текстом, если значение не проходит.
        """
        changes: list[tuple[str, Any, Any]] = []
        for ref in self.fields:
            if not ref.editable:
                continue
            widget = self.query_one(f"#{_widget_id(ref.path)}")
            old = get_path(target, ref.path)
            new = self._read(ref, widget)
            if ref.field.type in FLOAT_TYPES and abs(new - old) <= 1e-6 * max(1.0, abs(old)):
                continue  # float32 на устройстве не совпадает бит в бит с вводом
            if new != old:
                try:
                    set_path(target, ref.path, new)
                except (ValueError, TypeError) as e:
                    raise ValueError(f"{ref.path}: {e}") from None
                changes.append((ref.path, old, new))
        return changes

    def describe(self, path: str, value: Any) -> str:
        """Значение поля для показа человеку: имя enum, скрытые секреты."""
        ref = next((r for r in self.fields if r.path == path), None)
        if ref is None:
            return str(value)
        if path.endswith(SECRET_SUFFIXES):
            return "••••" if value else "—"
        if ref.field.type == FD.TYPE_ENUM:
            ev = ref.field.enum_type.values_by_number.get(value)
            return ev.name if ev else str(value)
        return repr(value) if isinstance(value, str) else str(value)

    @staticmethod
    def _read(ref: FieldRef, widget: Any) -> Any:
        f = ref.field
        if isinstance(widget, Switch):
            return widget.value
        if isinstance(widget, Select):
            return widget.value
        text = widget.value.strip()
        if f.type in INT_TYPES:
            try:
                n = int(text or "0")
            except ValueError:
                raise ValueError(f"{ref.path}: ожидается целое число") from None
            if n < 0 and f.type in UNSIGNED_TYPES:
                raise ValueError(f"{ref.path}: отрицательное значение недопустимо")
            return n
        if f.type in FLOAT_TYPES:
            try:
                return float(text or "0")
            except ValueError:
                raise ValueError(f"{ref.path}: ожидается число") from None
        return widget.value
