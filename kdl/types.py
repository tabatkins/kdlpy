from __future__ import annotations

import base64
import dataclasses
from dataclasses import dataclass
import datetime
import decimal
import ipaddress
import math
import re
import urllib.parse
import uuid

from . import printing, t
from .chars import NL_SET, isBareIdent

if t.TYPE_CHECKING:
    VT = t.TypeVar("VT")
    LooseEntry = tuple[str | None, t.Any]


def _entrySortKey(entry: tuple[str | None, t.Any]) -> str:
    return entry[0] or ""


# Precomputed indentation strings per indent unit, grown lazily for deep trees.
_INITIAL_INDENT_DEPTH = 16
_INDENTS: dict[str, list[str]] = {
    unit: [unit * i for i in range(_INITIAL_INDENT_DEPTH)] for unit in ("\t", "    ", "  ", "")
}


def _getIndents(unit: str, minLevel: int = 0) -> list[str]:
    indents = _INDENTS.get(unit)
    if indents is None:
        depth = max(_INITIAL_INDENT_DEPTH, minLevel + 1)
        indents = [unit * i for i in range(depth)]
        _INDENTS[unit] = indents
    elif minLevel >= len(indents):
        indents.extend(unit * i for i in range(len(indents), minLevel + _INITIAL_INDENT_DEPTH))
    return indents


@dataclass
class Document:
    nodes: list[Node] = dataclasses.field(default_factory=list)
    printConfig: t.PrintConfig | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or self.printConfig or printing.defaults
        if not self.nodes:
            # always end a kdl doc with a newline
            return "\n"
        out: list[str] = []
        indents = _getIndents(cfg.indent)
        for node in self.nodes:
            childNode = node if isinstance(node, Node) else toKdlNode(node)
            _printNodeInto(childNode, out, cfg, 0, indents)
        if not out:
            return "\n"
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None:  # noqa: F811
        ...

    @t.overload
    def get(self, key: t.NodeKey, default: VT) -> Node | VT:  # noqa: F811
        ...

    def get(  # noqa: F811
        self,
        key: t.NodeKey,
        default: VT | None = None,
    ) -> Node | VT | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        for node in self.nodes:
            if node.matchesKey(key):
                return node
        raise KeyError(key)

    def getAll(
        self,
        key: t.NodeKey,
    ) -> t.Iterable[Node]:
        for node in self.nodes:
            if nodeMatchesKey(node, key):
                yield node

    def __str__(self) -> str:
        return self.print()


@dataclass
class Node:
    name: str
    tag: str | None = None
    entries: list[LooseEntry] = dataclasses.field(default_factory=list)
    nodes: list[Node] = dataclasses.field(default_factory=list)

    def print(
        self,
        config: t.PrintConfig | None = None,
        indentLevel: int = 0,
    ) -> str:
        cfg = config if config is not None else printing.defaults
        level = max(0, indentLevel)
        out: list[str] = []
        indents = _getIndents(cfg.indent, level)
        _printNodeInto(self, out, cfg, level, indents)
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None:  # noqa: F811
        ...

    @t.overload
    def get(self, key: t.NodeKey, default: VT) -> Node | VT:  # noqa: F811
        ...

    def get(  # noqa: F811
        self,
        key: t.NodeKey,
        default: VT | None = None,
    ) -> Node | VT | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        for node in self.nodes:
            if node.matchesKey(key):
                return node
        raise KeyError(key)

    def getAll(
        self,
        key: t.NodeKey,
    ) -> t.Iterable[Node]:
        for node in self.nodes:
            if nodeMatchesKey(node, key):
                yield node

    def matchesKey(self, key: t.NodeKey) -> bool:
        return nodeMatchesKey(self, key)

    def getEntries(self, key: t.ValueKey) -> t.Iterable[LooseEntry]:
        for name, val in self.entries:
            if valueMatchesKey(val, key):
                yield name, val

    def getProps(self, key: t.ValueKey) -> t.Iterable[tuple[str, t.Any]]:
        for name, val in self.entries:
            if name is not None and valueMatchesKey(val, key):
                yield name, val

    def getArgs(self, key: t.ValueKey) -> t.Iterable[t.Any]:
        for name, val in self.entries:
            if name is None and valueMatchesKey(val, key):
                yield val

    def __str__(self) -> str:
        return self.print()


def _printNodeInto(
    node: Node,
    out: list[str],
    config: t.PrintConfig,
    indentLevel: int,
    indents: list[str],
) -> None:
    # Appends chunks into a single shared list to avoid O(N^2) string concatenation across subtrees.
    append = out.append
    identCache = _IDENT_CACHE
    indentStr = indents[indentLevel]

    # Inline cache lookup is safe with `or` because printIdent() never returns "".
    nameStr = identCache.get(node.name) or printIdent(node.name)
    if node.tag is not None:
        tagStr = identCache.get(node.tag) or printIdent(node.tag)
        append(f"{indentStr}({tagStr}){nameStr}")
    else:
        append(f"{indentStr}{nameStr}" if indentStr else nameStr)

    entries: t.Iterable[tuple[str | None, t.Any]]
    if config.sortEntries:
        entries = sorted(node.entries, key=_entrySortKey)
    else:
        entries = node.entries

    printNulls = config.printNulls
    for name, value in entries:
        # Exact `type(value) is ...` avoids isinstance() MRO checks for plain primitives;
        # subclasses (e.g. IntEnum) still fall through to toKdlValue().
        valType = type(value)
        if valType is str:
            valStr = identCache.get(value) or printIdent(value)
        elif value is True:
            valStr = "#true"
        elif value is False:
            valStr = "#false"
        elif valType is float:
            if math.isfinite(value):
                valStr = str(value)
            elif math.isnan(value):
                valStr = "#nan"
            else:
                valStr = "#inf" if value > 0 else "#-inf"
        elif valType is int:
            valStr = str(value)
        elif value is None:
            if not printNulls:
                continue
            valStr = "#null"
        elif isinstance(value, Value):
            if not printNulls and isinstance(value, Null):
                continue
            valStr = value.print(config)
        else:
            kdlVal = toKdlValue(value)
            if not printNulls and isinstance(kdlVal, Null):
                continue
            valStr = kdlVal.print(config)

        if name is None:
            append(f" {valStr}")
        else:
            keyStr = identCache.get(name) or printIdent(name)
            append(f" {keyStr}={valStr}")

    if node.nodes:
        append(" {\n")
        childLevel = indentLevel + 1
        if childLevel >= len(indents):
            unit = config.indent
            indents.extend(unit * i for i in range(len(indents), childLevel + _INITIAL_INDENT_DEPTH))
        for child in node.nodes:
            childNode = child if isinstance(child, Node) else toKdlNode(child)
            _printNodeInto(childNode, out, config, childLevel, indents)
        if config.semicolons:
            append(f"{indentStr}}};\n" if indentStr else "};\n")
        else:
            append(f"{indentStr}}}\n" if indentStr else "}\n")
    else:
        append(";\n" if config.semicolons else "\n")


class Value:
    # Plain base class (not ABCMeta) so isinstance(x, Value/Numberish/Stringish) uses C's fast MRO check.
    value: t.Any
    tag: str | None

    def print(self, config: t.PrintConfig | None = None) -> str:
        raise NotImplementedError

    def matchesKey(self, key: t.ValueKey) -> bool:
        return valueMatchesKey(self, key)


@dataclass
class ExactValue(Value):
    # Not produced by anything in the parser,
    # but used when a native type needs a precise output
    # not captured by the native semantics,
    # like precise number formatting.
    # .chars *must* be a valid KDL value
    chars: str
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return self.chars
        return f"({printIdent(self.tag)}){self.chars}"

    def __str__(self) -> str:
        return self.print()


class Numberish(Value):  # pylint: disable=abstract-method
    pass


@dataclass
class Binary(Numberish):
    value: int
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config if config is not None else printing.defaults
        numStr = bin(self.value) if cfg.respectRadix else str(self.value)
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Octal(Numberish):
    value: int
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config if config is not None else printing.defaults
        numStr = oct(self.value) if cfg.respectRadix else str(self.value)
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Decimal(Numberish):
    mantissa: int | float
    exponent: int = 0
    tag: str | None = None

    @property
    def value(self) -> float:
        if self.exponent == 0:
            return float(self.mantissa)
        # Parse via strtod rather than `mantissa * 10.0**exponent` to avoid OverflowError and double-rounding.
        return float(f"{self.mantissa}e{self.exponent}")

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config if config is not None else printing.defaults
        s = str(self.mantissa)
        if self.exponent != 0:
            sign = "+" if self.exponent > 0 else ""
            s = f"{s}{cfg.exponent}{sign}{self.exponent}"
        if self.tag is None:
            return s
        return f"({printIdent(self.tag)}){s}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Infinity(Numberish):
    value: float
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        kw = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return kw
        return f"({printIdent(self.tag)}){kw}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class NaN(Numberish):
    value: float = float("nan")
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return "#nan"
        return f"({printIdent(self.tag)})#nan"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Hex(Numberish):
    value: int
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config if config is not None else printing.defaults
        numStr = hex(self.value) if cfg.respectRadix else str(self.value)
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Bool(Value):
    value: bool
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        kw = "#true" if self.value else "#false"
        if self.tag is None:
            return kw
        return f"({printIdent(self.tag)}){kw}"

    def __str__(self) -> str:
        return self.print()


@dataclass
class Null(Value):
    tag: str | None = None

    @property
    def value(self) -> None:
        return None

    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return "#null"
        return f"({printIdent(self.tag)})#null"

    def __str__(self) -> str:
        return self.print()


class Stringish(Value):  # pylint: disable=abstract-method
    pass


@dataclass
class RawString(Stringish):
    value: str
    tag: str | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config if config is not None else printing.defaults
        tagStr = f"({printIdent(self.tag)})" if self.tag is not None else ""
        if cfg.respectStringType and not any(ch in NL_SET for ch in self.value):
            hashes = "#" * findRequiredHashCount(self.value)
            return f'{tagStr}{hashes}"{self.value}"{hashes}'
        return f'{tagStr}"{escapedFromRaw(self.value)}"'

    def __str__(self) -> str:
        return self.print()


def findRequiredHashCount(chars: str) -> int:
    if '"' not in chars:
        return 1
    for i in range(1, 100):
        ender = '"' + ("#" * i)
        if ender not in chars:
            return i
    msg = "A raw string requires more than 100 hashes???"
    raise ValueError(msg)


@dataclass
class String(Stringish):
    value: str
    tag: str | None = None
    multiline: bool = False

    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.multiline:
            return ""
        body = _IDENT_CACHE.get(self.value) or printIdent(self.value)
        if self.tag is None:
            return body
        tagStr = _IDENT_CACHE.get(self.tag) or printIdent(self.tag)
        return f"({tagStr}){body}"

    def __str__(self) -> str:
        return self.print()


_KDLISH_TYPES = (
    str,
    int,
    float,
    bool,
    decimal.Decimal,
    datetime.time,
    datetime.date,
    datetime.datetime,
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    urllib.parse.ParseResult,
    uuid.UUID,
    re.Pattern,
    bytes,
    Value,
)


def toKdlNode(val: t.Any) -> Node:
    if isinstance(val, Node):
        return val
    toKdl = getattr(val, "to_kdl", None)
    if not callable(toKdl):
        msg = f"Can't convert object to KDL for serialization. Got:\n{val!r}"
        raise Exception(msg)
    node = toKdl()
    if not isinstance(node, Node):
        msg = f"Expected object to convert to KDL Node. Got:\n{val!r}"
        raise Exception(msg)
    return node


def toKdlValue(val: t.Any) -> t.KDLValue:
    """
    Converts any KDLish value (a KDLValue, a primitive,
    an object corresponding to a built-in tag,
    or an object with .to_kdl() that returns one of the above)
    into a KDLValue
    """
    if isinstance(val, Value):
        return val
    if val is None:
        return Null()
    if isinstance(val, bool):
        return Bool(val)
    if isinstance(val, str):
        return String(val)
    if isinstance(val, int):
        return Decimal(val)
    if isinstance(val, float):
        if math.isfinite(val):
            return Decimal(val)
        if math.isnan(val):
            return NaN(val)
        return Infinity(val)
    if isinstance(val, decimal.Decimal):
        return String(str(val), "decimal")
    if isinstance(val, datetime.datetime):
        return String(val.isoformat(), "date-time")
    if isinstance(val, datetime.time):
        return String(val.isoformat(), "time")
    if isinstance(val, datetime.date):
        return String(val.isoformat(), "date")
    if isinstance(val, ipaddress.IPv4Address):
        return String(str(val), "ipv4")
    if isinstance(val, ipaddress.IPv6Address):
        return String(str(val), "ipv6")
    if isinstance(val, urllib.parse.ParseResult):
        return String(urllib.parse.urlunparse(val), "url")
    if isinstance(val, uuid.UUID):
        return String(str(val), "uuid")
    if isinstance(val, re.Pattern):
        return RawString(val.pattern, "regex")
    if isinstance(val, bytes):
        return String(base64.b64encode(val).decode("utf-8"), "base64")

    toKdl = getattr(val, "to_kdl", None)
    if not callable(toKdl):
        msg = f"Can't convert object to KDL for serialization. Got:\n{val!r}"
        raise Exception(msg)

    convertedVal = toKdl()
    if isKdlishValue(convertedVal):
        return toKdlValue(convertedVal)
    msg = f"Expected object to convert to KDL value or compatible primitive. Got:\n{val!r}"
    raise Exception(msg)


def isKdlishValue(val: t.Any) -> bool:
    if val is None:
        return True
    return isinstance(val, _KDLISH_TYPES)


def printTag(tag: str | None) -> str:
    if tag is not None:
        return f"({printIdent(tag)})"
    return ""


def nodeMatchesKey(node: t.Any, key: t.NodeKey) -> bool:
    if not isinstance(node, Node):
        node = node.to_kdl()
    if isinstance(key, tuple):
        tagKey, nameKey = key
        return tagMatchesKey(node.tag, tagKey) and nameMatchesKey(node.name, nameKey)
    return nameMatchesKey(node.name, key)


def valueMatchesKey(value: t.Any, key: t.ValueKey) -> bool:
    # Need to allow for both Value and values that were converted to other types
    # non-Value objects are untagged, by definition
    tag = value.tag if isinstance(value, Value) else None
    if isinstance(key, tuple):
        tagKey, typeKey = key
        return tagMatchesKey(tag, tagKey) and typeMatchesKey(value, typeKey)
    return tagMatchesKey(tag, key)


def tagMatchesKey(val: str | None, key: t.TagKey) -> bool:
    if key is Ellipsis:
        return True
    if key is None:
        return val is None
    if isinstance(key, str):
        return val == key
    if isinstance(key, re.Pattern):
        if val is None:
            return False
        return bool(key.match(val))
    if callable(key):
        return bool(key(val))
    msg = f"Invalid TagKey {key!r}"
    raise Exception(msg)


def nameMatchesKey(val: str | None, key: t.NameKey) -> bool:
    if key is Ellipsis or key is None:
        return True
    if isinstance(key, str):
        return val == key
    if isinstance(key, re.Pattern):
        if val is None:
            return False
        return bool(key.match(val))
    if callable(key):
        return bool(key(val))
    msg = f"Invalid NameKey {key!r}"
    raise Exception(msg)


def typeMatchesKey(val: t.Any, key: t.TypeKey) -> bool:
    if key is Ellipsis:
        return True
    try:
        return isinstance(val, t.cast("t._ClassInfo", key))
    except Exception as e:
        msg = f"Invalid TypeKey {key!r}"
        raise Exception(msg) from e


def escapedFromRaw(chars: str) -> str:
    return (
        chars.replace("\\", "\\\\")
        .replace('"', '\\"')
        # don't escape a forward slash when printing
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


# Bounded cache of formatted idents (always non-empty strings, so callers can use `cache.get(s) or printIdent(s)`).
_IDENT_CACHE: dict[str, str] = {}


def printIdent(chars: str) -> str:
    cached = _IDENT_CACHE.get(chars)
    if cached is not None:
        return cached
    res = chars if isBareIdent(chars) else f'"{escapedFromRaw(chars)}"'
    # Only cache short strings and flush at 4096 entries to cap memory on large/unbounded documents.
    if len(chars) <= 64:
        if len(_IDENT_CACHE) >= 4096:
            _IDENT_CACHE.clear()
        _IDENT_CACHE[chars] = res
    return res
