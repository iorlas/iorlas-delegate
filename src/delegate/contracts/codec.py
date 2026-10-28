"""One codec for every record: dataclass <-> JSON-able dict, driven by the class's type hints (read once per class).

A record class is a dataclass. `V` (class attribute) is its wire version: `to_json` writes it as `v`, `from_json` refuses any
other with StaleRecord. `KIND` (class attribute) is the tag of a union member, written under `TAG` (default `kind`). A field renamed on the wire
(`in` is a keyword) says so with `field(metadata={"name": "in"})`. A scalar class with `to_wire()` / `from_wire()` (RunId,
Version) is stored as that wire value. Unknown keys, missing required keys and wrong types raise CodecError: no coercion.
"""
import dataclasses
import enum
import functools
import types
import typing
from typing import Any, Literal, Union, get_args, get_origin


class Absent:
    """The default of a field the wire leaves out when nobody set it (not the same as null)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ABSENT"


ABSENT = Absent()


class CodecError(ValueError):
    """The data is not this record."""


class StaleRecord(CodecError):
    """The data carries another `v` than the class: the record is treated as absent, never migrated."""

    def __init__(self, name: str, found: object, want: int) -> None:
        super().__init__("%s: v=%r, this code reads v=%d" % (name, found, want))
        self.found = found
        self.want = want


def record[T](cls: type[T]) -> type[T]:
    """The decorator of every record: a frozen, keyword-only, slotted dataclass."""
    return dataclasses.dataclass(frozen=True, kw_only=True, slots=True)(cls)


def mutable[T](cls: type[T]) -> type[T]:
    """A record a process fills in as it goes (the reaper's report): a keyword-only, slotted dataclass."""
    return dataclasses.dataclass(kw_only=True, slots=True)(cls)


@dataclasses.dataclass(frozen=True)
class FieldPlan:
    attr: str
    wire: str
    hint: Any
    required: bool


@functools.cache
def plan(cls: type) -> tuple[FieldPlan, ...]:
    """The class's fields with resolved hints; get_type_hints runs once per class."""
    hints = typing.get_type_hints(cls)
    out = []
    for f in dataclasses.fields(cls):
        has_default = f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
        out.append(FieldPlan(f.name, f.metadata.get("name", f.name), hints[f.name], not has_default))
    return tuple(out)


def to_json(obj: Any) -> dict[str, Any]:
    """The record as a dict: `v` and `kind` first when the class has them, then its fields in declaration order."""
    return _dump(obj, True)


def _dump(obj: Any, top: bool) -> dict[str, Any]:
    cls = type(obj)
    out: dict[str, Any] = {}
    if top and getattr(cls, "V", None) is not None:
        out["v"] = cls.V
    if getattr(cls, "KIND", None) is not None:
        out[getattr(cls, "TAG", "kind")] = cls.KIND
    for p in plan(cls):
        value = getattr(obj, p.attr)
        if value is not ABSENT:
            out[p.wire] = _enc(value)
    return out


def _enc(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return _dump(v, False)
    if isinstance(v, enum.Enum):
        return v.value
    if hasattr(v, "to_wire"):
        return v.to_wire()
    if isinstance(v, (list, tuple)):
        return [_enc(x) for x in v]
    if isinstance(v, (set, frozenset)):
        return sorted(_enc(x) for x in v)
    if isinstance(v, dict):
        return {str(k): _enc(x) for k, x in v.items()}
    return v


def from_json[T](cls: type[T], data: Any) -> T:
    """The record of class `cls` from a dict; StaleRecord when its `v` is not the class's."""
    if not isinstance(data, dict):
        raise CodecError("%s: expected an object, got %s" % (cls.__name__, type(data).__name__))
    want = getattr(cls, "V", None)
    if want is not None and data.get("v") != want:
        raise StaleRecord(cls.__name__, data.get("v"), want)
    return _load(cls, data, cls.__name__, {"v"} if want is not None else set())


def table_of(*classes: type) -> dict[str, type]:
    """{KIND: class} of a union's members."""
    out = {c.KIND: c for c in classes}
    if len(out) != len(classes):
        raise ValueError("two members of one union share a KIND")
    return out


def from_tagged(table: dict[str, type], data: Any, tag: str = "kind") -> Any:
    """The union member `data[tag]` names. An unknown kind is a CodecError."""
    if not isinstance(data, dict):
        raise CodecError("expected an object, got %s" % type(data).__name__)
    kind = data.get(tag)
    cls = table.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise CodecError("unknown kind %r" % (kind,))
    return from_json(cls, data)


def _load(cls: type, data: dict[str, Any], path: str, skip: set[str]) -> Any:
    kind = getattr(cls, "KIND", None)
    if kind is not None:
        tag = getattr(cls, "TAG", "kind")
        if data.get(tag) != kind:
            raise CodecError("%s: %s %r, expected %r" % (path, tag, data.get(tag), kind))
        skip = skip | {tag}
    plans = plan(cls)
    extra = set(data) - skip - {p.wire for p in plans}
    if extra:
        raise CodecError("%s: unknown key %s" % (path, ", ".join(sorted(extra))))
    kwargs = {}
    for p in plans:
        if p.wire in data:
            kwargs[p.attr] = _dec(p.hint, data[p.wire], "%s.%s" % (path, p.wire))
        elif p.required:
            raise CodecError("%s.%s: missing" % (path, p.wire))
    return cls(**kwargs)


def _dec(h: Any, v: Any, path: str) -> Any:
    if h is Any:
        return v
    if h is None or h is type(None):
        if v is None:
            return None
        raise CodecError("%s: expected null" % path)
    sup = getattr(h, "__supertype__", None)
    if sup is not None:
        return _dec(sup, v, path)
    origin = get_origin(h)
    if origin is None:
        return _dec_plain(h, v, path)
    return _dec_generic(h, origin, v, path)


def _dec_plain(h: Any, v: Any, path: str) -> Any:
    if not isinstance(h, type):
        raise CodecError("%s: unsupported type %r" % (path, h))
    if dataclasses.is_dataclass(h):
        if not isinstance(v, dict):
            raise CodecError("%s: expected an object" % path)
        return _load(h, v, path, set())
    if issubclass(h, enum.Enum):
        try:
            return h(v)
        except ValueError:
            raise CodecError("%s: %r is not a %s" % (path, v, h.__name__)) from None
    if hasattr(h, "from_wire"):
        try:
            return h.from_wire(v)
        except (ValueError, TypeError) as e:
            raise CodecError("%s: %s" % (path, e)) from None
    if h is float and isinstance(v, (int, float)) and not isinstance(v, bool):
        return v        # an int stays an int: the bytes the wire carried are the bytes written back
    if h is int and isinstance(v, bool):
        raise CodecError("%s: expected int, got bool" % path)
    if h in (str, int, float, bool) and type(v) is h:
        return v
    raise CodecError("%s: expected %s, got %s" % (path, h.__name__, type(v).__name__))


def _dec_generic(h: Any, origin: Any, v: Any, path: str) -> Any:
    args = get_args(h)
    if origin is Union or origin is types.UnionType:
        if v is None and type(None) in args:
            return None
        errs = []
        for a in args:
            if a is type(None) or a is Absent:
                continue
            try:
                return _dec(a, v, path)
            except CodecError as e:
                errs.append(str(e))
        raise CodecError("%s: matches no member of the union (%s)" % (path, "; ".join(errs)))
    if origin is Literal:
        if any(a == v and type(a) is type(v) for a in args):
            return v
        raise CodecError("%s: %r is not one of %s" % (path, v, list(args)))
    if origin in (list, set, frozenset, tuple):
        if not isinstance(v, list):
            raise CodecError("%s: expected an array" % path)
        if origin is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            if len(v) != len(args):
                raise CodecError("%s: expected %d items" % (path, len(args)))
            return tuple(_dec(a, x, "%s[%d]" % (path, i)) for i, (a, x) in enumerate(zip(args, v)))
        items = [_dec(args[0], x, "%s[%d]" % (path, i)) for i, x in enumerate(v)]
        return items if origin is list else tuple(items) if origin is tuple else origin(items)
    if origin is dict:
        if not isinstance(v, dict):
            raise CodecError("%s: expected an object" % path)
        return {k: _dec(args[1], x, "%s.%s" % (path, k)) for k, x in v.items()}
    raise CodecError("%s: unsupported type %r" % (path, h))
