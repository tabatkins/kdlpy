from __future__ import annotations

import base64
import dataclasses
import datetime
import decimal
import ipaddress
import math
import re
import urllib.parse
import uuid
from dataclasses import dataclass

from . import printing, t
from .chars import DISALLOWED_OR_NL_PATTERN, DISALLOWED_OR_NON_ASCII_NL_RE, NEG_INF, POS_INF, isBareIdent

# Cache of precomputed indentation strings per indent unit, populated lazily on first use.
_INITIAL_INDENT_DEPTH = 16
_INDENTS: dict[str, list[str]] = {}


def _getIndents(unit: str, minLevel: int = 0) -> list[str]:
    indents = _INDENTS.get(unit)
    if indents is None:
        depth = max(_INITIAL_INDENT_DEPTH, minLevel + 1)
        indents = _INDENTS[unit] = [unit * i for i in range(depth)]
    elif minLevel >= len(indents):
        indents.extend(unit * i for i in range(len(indents), minLevel + _INITIAL_INDENT_DEPTH))
    return indents


@dataclass(slots=True)
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
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None: ...

    @t.overload
    def get[VT](self, key: t.NodeKey, default: VT) -> Node | VT: ...

    def get[VT](self, key: t.NodeKey, default: VT | None = None) -> Node | VT | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        for node in self.nodes:
            if nodeMatchesKey(node, key):
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


@dataclass(slots=True)
class Node:
    name: str
    tag: str | None = None
    args: list[t.Any] = dataclasses.field(default_factory=list)
    props: dict[str, t.Any] = dataclasses.field(default_factory=dict)
    nodes: list[Node] = dataclasses.field(default_factory=list)

    def print(
        self,
        config: t.PrintConfig | None = None,
        indentLevel: int = 0,
    ) -> str:
        cfg = config or printing.defaults
        level = max(0, indentLevel)
        out: list[str] = []
        indents = _getIndents(cfg.indent, level)
        _printNodeInto(self, out, cfg, level, indents)
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None: ...

    @t.overload
    def get[VT](self, key: t.NodeKey, default: VT) -> Node | VT: ...

    def get[VT](self, key: t.NodeKey, default: VT | None = None) -> Node | VT | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        for node in self.nodes:
            if nodeMatchesKey(node, key):
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

    def getProps(self, key: t.ValueKey) -> t.Iterable[tuple[str, t.Any]]:
        for name, val in self.props.items():
            if valueMatchesKey(val, key):
                yield name, val

    def getArgs(self, key: t.ValueKey) -> t.Iterable[t.Any]:
        for val in self.args:
            if valueMatchesKey(val, key):
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

    if node.args:
        printNullArgs = config.printNullArgs
        for value in node.args:
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
                try:
                    valStr = str(value)
                except ValueError:
                    valStr = "#inf" if value > 0 else "#-inf"
            elif value is None:
                if not printNullArgs:
                    continue
                valStr = "#null"
            elif isinstance(value, Value):
                if not printNullArgs and isinstance(value, Null):
                    continue
                valStr = value.print(config)
            else:
                kdlVal = toKdlValue(value)
                if not printNullArgs and isinstance(kdlVal, Null):
                    continue
                valStr = kdlVal.print(config)
            append(f" {valStr}")

    if node.props:
        propsIter: t.Iterable[tuple[str, t.Any]] = (
            sorted(node.props.items()) if config.sortProperties else node.props.items()
        )
        printNullProps = config.printNullProps
        for name, value in propsIter:
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
                try:
                    valStr = str(value)
                except ValueError:
                    valStr = "#inf" if value > 0 else "#-inf"
            elif value is None:
                if not printNullProps:
                    continue
                valStr = "#null"
            elif isinstance(value, Value):
                if not printNullProps and isinstance(value, Null):
                    continue
                valStr = value.print(config)
            else:
                kdlVal = toKdlValue(value)
                if not printNullProps and isinstance(kdlVal, Null):
                    continue
                valStr = kdlVal.print(config)
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
    __slots__ = ("tag",)
    tag: str | None

    @property
    def value(self) -> t.Any:
        raise NotImplementedError

    def print(self, config: t.PrintConfig | None = None) -> str:
        raise NotImplementedError

    def matchesKey(self, key: t.ValueKey) -> bool:
        return valueMatchesKey(self, key)


@dataclass(slots=True)
class ExactValue(Value):
    # Not produced by anything in the parser,
    # but used when a native type needs a precise output
    # not captured by the native semantics,
    # like precise number formatting.
    # .chars *must* be a valid KDL value
    chars: str
    tag: str | None = None

    @property
    @t.override
    def value(self) -> str:
        return self.chars

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return self.chars
        return f"({printIdent(self.tag)}){self.chars}"

    def __str__(self) -> str:
        return self.print()


class Numberish(Value):  # pylint: disable=abstract-method
    __slots__ = ()


@dataclass(slots=True)
class Binary(Numberish):
    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or printing.defaults
        if cfg.respectRadix:
            numStr = bin(self.value)
        else:
            try:
                numStr = str(self.value)
            except ValueError:
                numStr = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Octal(Numberish):
    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or printing.defaults
        if cfg.respectRadix:
            numStr = oct(self.value)
        else:
            try:
                numStr = str(self.value)
            except ValueError:
                numStr = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Decimal(Numberish):
    mantissa: int | float
    exponent: int = 0
    tag: str | None = None

    @property
    @t.override
    def value(self) -> float:
        mantissa = self.mantissa
        if isinstance(mantissa, float) and not math.isfinite(mantissa):
            return mantissa
        if self.exponent == 0:
            if isinstance(mantissa, float):
                return mantissa
            try:
                return float(mantissa)
            except OverflowError:
                return POS_INF if mantissa > 0 else NEG_INF
        # Parse via strtod rather than `mantissa * 10.0**exponent` to avoid OverflowError and double-rounding.
        try:
            s = str(mantissa)
        except ValueError:
            return POS_INF if mantissa > 0 else NEG_INF
        if "e" in s:
            # Guard against the mantissa itself printing with scinot.
            base, mExp = s.split("e")
            return float(f"{base}e{int(mExp) + self.exponent}")
        return float(f"{s}e{self.exponent}")

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or printing.defaults
        mantissa = self.mantissa
        if isinstance(mantissa, float) and not math.isfinite(mantissa):
            s = "#nan" if math.isnan(mantissa) else ("#inf" if mantissa > 0 else "#-inf")
        else:
            try:
                s = str(mantissa)
            except ValueError:
                s = "#inf" if mantissa > 0 else "#-inf"
            else:
                exp = self.exponent
                if "e" in s:
                    s, mExp = s.split("e")
                    exp += int(mExp)
                if exp != 0:
                    s = f"{s}{cfg.exponent}{exp:+}"
        if self.tag is None:
            return s
        return f"({printIdent(self.tag)}){s}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Infinity(Numberish):
    value: float
    tag: str | None = None

    def __post_init__(self) -> None:
        if not math.isinf(self.value):
            msg = f"Infinity value must be infinite; got {self.value!r}."
            raise ValueError(msg)

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        if not math.isinf(self.value):
            msg = f"Infinity value must be infinite; got {self.value!r}."
            raise ValueError(msg)
        kw = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return kw
        return f"({printIdent(self.tag)}){kw}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class NaN(Numberish):
    value: float = float("nan")
    tag: str | None = None

    def __post_init__(self) -> None:
        if not math.isnan(self.value):
            msg = f"NaN value must be NaN; got {self.value!r}."
            raise ValueError(msg)

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return "#nan"
        return f"({printIdent(self.tag)})#nan"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Hex(Numberish):
    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or printing.defaults
        if cfg.respectRadix:
            numStr = hex(self.value)
        else:
            try:
                numStr = str(self.value)
            except ValueError:
                numStr = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return numStr
        return f"({printIdent(self.tag)}){numStr}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Bool(Value):
    value: bool
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        kw = "#true" if self.value else "#false"
        if self.tag is None:
            return kw
        return f"({printIdent(self.tag)}){kw}"

    def __str__(self) -> str:
        return self.print()


@dataclass(slots=True)
class Null(Value):
    tag: str | None = None

    @property
    @t.override
    def value(self) -> None:
        return None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        if self.tag is None:
            return "#null"
        return f"({printIdent(self.tag)})#null"

    def __str__(self) -> str:
        return self.print()


class Stringish(Value):  # pylint: disable=abstract-method
    __slots__ = ()


_searchRawDisallowed = DISALLOWED_OR_NL_PATTERN.search
_findQuoteHashRuns = re.compile(r'"(#*)').finditer


@dataclass(slots=True)
class RawString(Stringish):
    value: str
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        cfg = config or printing.defaults
        tagStr = f"({printIdent(self.tag)})" if self.tag is not None else ""
        # Leading quotes (e.g. `"` -> `#"""#`), newlines, or disallowed codepoints can't be represented as
        # single-line KDL2 raw strings, so fall back to escaped quoted strings.
        if (
            cfg.respectStringType
            and not self.value.startswith('"')
            and (self.value.isprintable() or _searchRawDisallowed(self.value) is None)
        ):
            hashes = "#" * findRequiredHashCount(self.value)
            return f'{tagStr}{hashes}"{self.value}"{hashes}'
        return f'{tagStr}"{escapedFromRaw(self.value)}"'

    def __str__(self) -> str:
        return self.print()


def findRequiredHashCount(chars: str) -> int:
    if '"' not in chars:
        return 1
    return max((len(m.group(1)) for m in _findQuoteHashRuns(chars)), default=0) + 1


@dataclass(slots=True)
class String(Stringish):
    value: str
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
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
    bytes,
    Value,
)


def toKdlNode(val: t.Any) -> Node:
    if isinstance(val, Node):
        return val
    toKdl = getattr(val, "to_kdl", None)
    if not callable(toKdl):
        msg = f"Can't convert object to KDL for serialization. Got:\n{val!r}"
        raise TypeError(msg)
    node = toKdl()
    if not isinstance(node, Node):
        msg = f"Expected object to convert to KDL Node. Got:\n{val!r}"
        raise TypeError(msg)
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
    if isinstance(val, re.Pattern) and isinstance(val.pattern, str):
        # Fast path for the most standard flag
        if val.flags != re.UNICODE:
            if val.flags != re.compile(val.pattern).flags:
                msg = (
                    "Can't serialize re.Pattern with non-default flags "
                    + "(use inline flags like (?i) in the pattern instead). "
                    + f"Got:\n{val!r}"
                )
                raise ValueError(msg)
        return RawString(val.pattern, "regex")
    if isinstance(val, bytes):
        return String(base64.b64encode(val).decode("utf-8"), "base64")

    toKdl = getattr(val, "to_kdl", None)
    if not callable(toKdl):
        msg = f"Can't convert object to KDL for serialization. Got:\n{val!r}"
        raise TypeError(msg)

    convertedVal = toKdl()
    if isKdlishValue(convertedVal):
        return toKdlValue(convertedVal)
    msg = f"Expected object to convert to KDL value or compatible primitive. Got:\n{val!r}"
    raise TypeError(msg)


def isKdlishValue(val: t.Any) -> bool:
    if val is None:
        return True
    if isinstance(val, re.Pattern):
        return isinstance(val.pattern, str)
    return isinstance(val, _KDLISH_TYPES)


def nodeMatchesKey(node: t.Any, key: t.NodeKey) -> bool:
    if not isinstance(node, Node):
        node = toKdlNode(node)
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid NodeKey {key!r}"
            raise TypeError(msg)
        tagKey, nameKey = key
        return tagMatchesKey(node.tag, tagKey) and nameMatchesKey(node.name, nameKey)
    return nameMatchesKey(node.name, key)


def valueMatchesKey(value: t.Any, key: t.ValueKey) -> bool:
    # Need to allow for both Value and values that were converted to other types
    # non-Value objects are untagged, by definition
    tag = value.tag if isinstance(value, Value) else None
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid ValueKey {key!r}"
            raise TypeError(msg)
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
    raise TypeError(msg)


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
    raise TypeError(msg)


def typeMatchesKey(val: t.Any, key: t.TypeKey) -> bool:
    if key is Ellipsis:
        return True
    try:
        return isinstance(val, t.cast("t._ClassInfo", key))
    except TypeError as e:
        msg = f"Invalid TypeKey {key!r}"
        raise TypeError(msg) from e


_UNICODE_ESCAPE_SUB = re.compile(DISALLOWED_OR_NON_ASCII_NL_RE).sub


def _unicodeEscapeChar(m: re.Match[str]) -> str:
    return f"\\u{{{ord(m.group()):x}}}"


def escapedFromRaw(chars: str) -> str:
    res = (
        chars.replace("\\", "\\\\")
        .replace('"', '\\"')
        # don't escape a forward slash when printing
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if not res.isprintable():
        res = _UNICODE_ESCAPE_SUB(_unicodeEscapeChar, res)
    return res


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
