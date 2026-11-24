from __future__ import annotations

import base64
import dataclasses
import datetime
import decimal
import ipaddress
import math
import re
import sys
import urllib.parse
import uuid
from dataclasses import dataclass

from . import printing, t
from .chars import DISALLOWED_OR_NL_PATTERN, DISALLOWED_OR_NON_ASCII_NL_RE, NEG_INF, POS_INF, isBareIdent

# Cache of precomputed indentation strings per indent unit, populated lazily on first use.
_INITIAL_INDENT_DEPTH = 16
_MAX_CACHED_INDENT_DEPTH = 64
_INDENTS: dict[str, list[str]] = {}


def _getIndents(unit: str, minLevel: int = 0) -> list[str]:
    """Return a cached list of repeated `unit` indent strings covering at least `minLevel`."""
    indents = _INDENTS.get(unit)
    if indents is None:
        depth = min(_MAX_CACHED_INDENT_DEPTH, max(_INITIAL_INDENT_DEPTH, minLevel + 1))
        indents = _INDENTS[unit] = [unit * i for i in range(depth)]
    elif minLevel >= len(indents) and len(indents) < _MAX_CACHED_INDENT_DEPTH:
        target = min(_MAX_CACHED_INDENT_DEPTH, minLevel + _INITIAL_INDENT_DEPTH)
        indents.extend(unit * i for i in range(len(indents), target))
    return indents


@dataclass(slots=True)
class Document:
    """Root KDL document containing a list of top-level nodes and an optional default `PrintConfig`."""

    nodes: list[Node] = dataclasses.field(default_factory=list)
    printConfig: t.PrintConfig | None = None

    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize this document to a KDL 2.0 string."""
        cfg = config or self.printConfig or printing.defaults
        strCache, strFmt = _getStrPrinter(cfg.respectStringType)
        if not self.nodes:
            # always end a kdl doc with a newline
            return "\n"
        out: list[str] = []
        state = _PrintState(out, cfg, strCache, strFmt)
        _printNodesInto(self.nodes, 0, state)
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None: ...

    @t.overload
    def get[VT](self, key: t.NodeKey, default: VT) -> Node | VT: ...

    def get[VT](self, key: t.NodeKey, default: VT | None = None) -> Node | VT | None:
        """Return the first top-level node matching `key`, or `default` if none matches."""
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        """Return the first top-level node matching `key`, raising KeyError/IndexError if absent."""
        return _getNodeByKey(self.nodes, key)

    def getAll(
        self,
        key: t.NodeKey,
    ) -> t.Iterable[Node]:
        """Yield all top-level nodes matching `key`."""
        return _iterNodesByKey(self.nodes, key)

    def __str__(self) -> str:
        """Serialize this document using its default print configuration."""
        return self.print()


@dataclass(slots=True)
class Node:
    """Single KDL node with a name, optional type tag, positional args, properties, and child nodes."""

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
        """Serialize this node and its subtree to a KDL 2.0 string at `indentLevel`."""
        cfg = config or printing.defaults
        strCache, strFmt = _getStrPrinter(cfg.respectStringType)
        level = max(0, indentLevel)
        out: list[str] = []
        state = _PrintState(out, cfg, strCache, strFmt)
        _printNodesInto([self], level, state)
        return "".join(out)

    @t.overload
    def get(self, key: t.NodeKey) -> Node | None: ...

    @t.overload
    def get[VT](self, key: t.NodeKey, default: VT) -> Node | VT: ...

    def get[VT](self, key: t.NodeKey, default: VT | None = None) -> Node | VT | None:
        """Return the first child node matching `key`, or `default` if none matches."""
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def __getitem__(self, key: t.NodeKey) -> Node:
        """Return the first child node matching `key`, raising KeyError/IndexError if absent."""
        return _getNodeByKey(self.nodes, key)

    def getAll(
        self,
        key: t.NodeKey,
    ) -> t.Iterable[Node]:
        """Yield all child nodes matching `key`."""
        return _iterNodesByKey(self.nodes, key)

    def matchesKey(self, key: t.NodeKey) -> bool:
        """Return True if this node matches `key`."""
        return nodeMatchesKey(self, key)

    def getProps(self, key: t.ValueKey) -> t.Iterable[tuple[str, t.Any]]:
        """Yield `(name, value)` pairs for properties whose values match `key`."""
        for name, val in self.props.items():
            if valueMatchesKey(val, key):
                yield name, val

    def getArgs(self, key: t.ValueKey) -> t.Iterable[t.Any]:
        """Yield positional argument values matching `key`."""
        for val in self.args:
            if valueMatchesKey(val, key):
                yield val

    def __call__(self, key: int | str) -> t.Any:
        """Shorthand for `getEntry(key)`."""
        return self.getEntry(key)

    def getEntry(self, key: int | str, default: t.Any = t.UNSET) -> t.Any:
        """Return property `key` (if `str`) or positional arg `key` (if `int`), or `default`."""
        if isinstance(key, str):
            if default is t.UNSET:
                return self.props[key]
            return self.props.get(key, default)
        if isinstance(key, int) and not isinstance(key, bool):
            try:
                return self.args[key]
            except IndexError:
                if default is t.UNSET:
                    raise
                return default
        msg = f"Entry key must be an int or str; got {type(key).__name__}."
        raise TypeError(msg)

    def setEntry(self, key: int | str, value: t.Any) -> None:
        """Set property `key` (if `str`) or positional arg `key` (if `int`, appending when `key == len(args)`)."""
        if isinstance(key, str):
            self.props[key] = value
            return
        if isinstance(key, int) and not isinstance(key, bool):
            if key == len(self.args):
                self.args.append(value)
            else:
                self.args[key] = value
            return
        msg = f"Entry key must be an int or str; got {type(key).__name__}."
        raise TypeError(msg)

    def __str__(self) -> str:
        """Serialize this node using the default print configuration."""
        return self.print()


class _PrintState(t.NamedTuple):
    """Shared output buffer and resolved string formatter passed to `_printNodesInto`."""

    out: list[str]
    config: t.PrintConfig
    strCache: dict[str, str]
    strFmt: t.Callable[[str], str]


def _printNodesInto(rootNodes: list[Node], baseLevel: int, state: _PrintState) -> None:
    """Iteratively serialize `rootNodes` into `state.out` using an explicit ancestor stack."""
    # Explicit stack of parent cursors avoids both O(N^2) string concat and recursion limits on deep trees.
    out, config, strCache, strFmt = state
    append = out.append
    strCacheGet = strCache.get
    printNullArgs = config.printNullArgs
    printNullProps = config.printNullProps
    sortProperties = config.sortProperties
    semicolons = config.semicolons
    indentUnit = config.indent
    indents = _getIndents(indentUnit, baseLevel)
    indentsLen = len(indents)

    # Resolve String value cache/formatter once so untagged `String` objects avoid per-value `.print()` calls.
    strValFmt: t.Callable[[str], str]
    if config.respectStringType is True:
        strValCacheGet = _QUOTED_STR_CACHE.get
        strValFmt = _printQuotedString
    else:
        strValCacheGet = strCacheGet
        strValFmt = strFmt

    isfinite = math.isfinite
    isnan = math.isnan

    # Cache several module-local variables to avoid even that crawl up the scope tree.
    nodeCls = Node
    valueCls = Value
    decCls = Decimal
    strValCls = String
    identStrCls = IdentString
    boolCls = Bool

    curNodes = rootNodes
    curIdx = 0
    curLen = len(curNodes)
    indentLevel = baseLevel
    indentStr = indents[indentLevel] if indentLevel < indentsLen else (indentUnit * indentLevel)
    # Saved ancestor cursors: (siblingNodes, nextIndex, len(siblingNodes), indentLevel, indentStr)
    stack: list[tuple[list[Node], int, int, int, str]] = []
    push = stack.append
    pop = stack.pop

    while True:
        while curIdx >= curLen:
            if not stack:
                return
            curNodes, curIdx, curLen, indentLevel, indentStr = pop()
            if semicolons:
                append(f"{indentStr}}};\n" if indentStr else "};\n")
            else:
                append(f"{indentStr}}}\n" if indentStr else "}\n")

        item = curNodes[curIdx]
        curIdx += 1
        node = item if isinstance(item, nodeCls) else _toKdlNode(item)

        # Inline cache lookup is safe with `or` because string formatters never return "".
        nameStr = strCacheGet(node.name) or strFmt(node.name)
        if node.tag is not None:
            tagStr = strCacheGet(node.tag) or strFmt(node.tag)
            append(f"{indentStr}({tagStr}){nameStr}")
        else:
            append(f"{indentStr}{nameStr}" if indentStr else nameStr)

        if node.args:
            for value in node.args:
                # Exact `type(value) is ...` avoids isinstance() MRO checks for plain primitives
                # and common untagged KDL Value wrappers; subclasses fall through cleanly.
                valType = type(value)
                if valType is str:
                    valStr = strCacheGet(value) or strFmt(value)
                elif valType is int:
                    try:
                        valStr = str(value)
                    except ValueError:
                        valStr = "#inf" if value > 0 else "#-inf"
                elif valType is float:
                    if isfinite(value):
                        valStr = str(value)
                    elif isnan(value):
                        valStr = "#nan"
                    else:
                        valStr = "#inf" if value > 0 else "#-inf"
                elif valType is bool:
                    valStr = "#true" if value else "#false"
                elif value is None:
                    if not printNullArgs:
                        continue
                    valStr = "#null"
                elif (
                    valType is decCls
                    and value.tag is None
                    and value.exponent == 0
                    and not isinstance(value.mantissa, float)
                ):
                    # Float mantissas stay on `.print()` so scinot floats respect `config.exponent`.
                    try:
                        valStr = str(value.mantissa)
                    except ValueError:
                        # Exceeds Python's int_max_str_digits limit; serialize as KDL infinity.
                        valStr = "#inf" if value.mantissa > 0 else "#-inf"
                elif valType is strValCls and value.tag is None:
                    valStr = strValCacheGet(value.value) or strValFmt(value.value)
                elif valType is boolCls and value.tag is None:
                    valStr = "#true" if value.value else "#false"
                elif valType is identStrCls and value.tag is None:
                    valStr = strCacheGet(value.value) or strFmt(value.value)
                elif isinstance(value, valueCls):
                    if not printNullArgs and isinstance(value, Null):
                        continue
                    valStr = value.print(config)
                else:
                    kdlVal = _toKdlValue(value)
                    if not printNullArgs and isinstance(kdlVal, Null):
                        continue
                    valStr = kdlVal.print(config)
                append(f" {valStr}")

        if node.props:
            propsIter: t.Iterable[tuple[str, t.Any]] = (
                sorted(node.props.items()) if sortProperties else node.props.items()
            )
            for name, value in propsIter:
                valType = type(value)
                if valType is str:
                    valStr = strCacheGet(value) or strFmt(value)
                elif valType is int:
                    try:
                        valStr = str(value)
                    except ValueError:
                        valStr = "#inf" if value > 0 else "#-inf"
                elif valType is float:
                    if isfinite(value):
                        valStr = str(value)
                    elif isnan(value):
                        valStr = "#nan"
                    else:
                        valStr = "#inf" if value > 0 else "#-inf"
                elif valType is bool:
                    valStr = "#true" if value else "#false"
                elif value is None:
                    if not printNullProps:
                        continue
                    valStr = "#null"
                elif (
                    valType is decCls
                    and value.tag is None
                    and value.exponent == 0
                    and not isinstance(value.mantissa, float)
                ):
                    # Float mantissas stay on `.print()` so scinot floats respect `config.exponent`.
                    try:
                        valStr = str(value.mantissa)
                    except ValueError:
                        # Exceeds Python's int_max_str_digits limit; serialize as KDL infinity.
                        valStr = "#inf" if value.mantissa > 0 else "#-inf"
                elif valType is strValCls and value.tag is None:
                    valStr = strValCacheGet(value.value) or strValFmt(value.value)
                elif valType is boolCls and value.tag is None:
                    valStr = "#true" if value.value else "#false"
                elif valType is identStrCls and value.tag is None:
                    valStr = strCacheGet(value.value) or strFmt(value.value)
                elif isinstance(value, valueCls):
                    if not printNullProps and isinstance(value, Null):
                        continue
                    valStr = value.print(config)
                else:
                    kdlVal = _toKdlValue(value)
                    if not printNullProps and isinstance(kdlVal, Null):
                        continue
                    valStr = kdlVal.print(config)
                keyStr = strCacheGet(name) or strFmt(name)
                append(f" {keyStr}={valStr}")

        if node.nodes:
            append(" {\n")
            push((curNodes, curIdx, curLen, indentLevel, indentStr))
            curNodes = node.nodes
            curIdx = 0
            curLen = len(curNodes)
            indentLevel += 1
            if indentLevel >= indentsLen and indentsLen < _MAX_CACHED_INDENT_DEPTH:
                _getIndents(indentUnit, indentLevel)
                indentsLen = len(indents)
            indentStr = indents[indentLevel] if indentLevel < indentsLen else (indentUnit * indentLevel)
        else:
            append(";\n" if semicolons else "\n")


class Value:
    """Base class for all explicit KDL value wrappers carrying an optional type `tag`."""

    # Plain base class (not ABCMeta) so isinstance(x, Value/Numberish/Stringish) uses C's fast MRO check.
    __slots__ = ("tag",)
    tag: str | None

    @property
    def value(self) -> t.Any:
        """Return the underlying Python native value."""
        raise NotImplementedError

    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize this value to its KDL 2.0 representation."""
        raise NotImplementedError

    def matchesKey(self, key: t.ValueKey) -> bool:
        """Return True if this value matches `key`."""
        return valueMatchesKey(self, key)


@dataclass(slots=True)
class ExactValue(Value):
    """Verbatim pre-formatted KDL literal for custom native types needing exact output control."""

    # Not produced by the parser; `.chars` must already be a valid KDL value literal.
    chars: str
    tag: str | None = None

    @property
    @t.override
    def value(self) -> str:
        """Return the verbatim KDL literal text."""
        return self.chars

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize `.chars` verbatim, prefixed with `(tag)` if tagged."""
        if self.tag is None:
            return self.chars
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)}){self.chars}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


class Numberish(Value):  # pylint: disable=abstract-method
    """Base class for all numeric KDL value wrappers (`Binary`, `Octal`, `Decimal`, `Hex`, `Infinity`, `NaN`)."""

    __slots__ = ()


@dataclass(slots=True)
class Binary(Numberish):
    """KDL binary integer literal (`0b...`)."""

    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `0b...` (when `respectRadix` is True) or decimal."""
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
        return f"({_printStr(self.tag, cfg.respectStringType)}){numStr}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class Octal(Numberish):
    """KDL octal integer literal (`0o...`)."""

    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `0o...` (when `respectRadix` is True) or decimal."""
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
        return f"({_printStr(self.tag, cfg.respectStringType)}){numStr}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


def _scaleInt(mantissa: int, exp: int) -> float | int:
    """Compute `mantissa * 10**exp` as an exact `int` if within str-digit limits, else ±inf."""
    if mantissa == 0:
        return 0
    # Python's minimum int-str-digits floor is 640, so small exponents/mantissas are always safe.
    if exp < 300 and -(10**100) < mantissa < 10**100:
        return t.cast("int", mantissa * (10**exp))
    maxDigits = sys.get_int_max_str_digits() or 4300
    # Estimate base-10 digits via log10(2) ≈ 0.30103 to avoid allocating enormous intermediate ints.
    if exp < maxDigits and (mantissa.bit_length() * 30103) // 100000 + exp < maxDigits:
        return t.cast("int", mantissa * (10**exp))
    return POS_INF if mantissa > 0 else NEG_INF


@dataclass(slots=True)
class Decimal(Numberish):
    """KDL decimal number storing `mantissa` and base-10 `exponent` separately for faithful round-tripping."""

    mantissa: int | float
    exponent: int = 0
    tag: str | None = None

    @property
    @t.override
    def value(self) -> float | int:
        """Evaluate `mantissa * 10**exponent` as a Python `int` or `float`."""
        mantissa = self.mantissa
        if isinstance(mantissa, float):
            if not math.isfinite(mantissa) or self.exponent == 0:
                return mantissa
        else:
            exp = self.exponent
            if exp == 0:
                return mantissa
            if exp > 0:
                return _scaleInt(mantissa, exp)
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
        """Serialize this decimal literal, formatting any non-zero exponent with `config.exponent`."""
        mantissa = self.mantissa
        exp = self.exponent
        if not isinstance(mantissa, float):
            try:
                s = str(mantissa)
            except ValueError:
                s = "#inf" if mantissa > 0 else "#-inf"
            else:
                if exp != 0:
                    cfg = config or printing.defaults
                    s = f"{s}{cfg.exponent}{exp:+}"
        elif not math.isfinite(mantissa):
            s = "#nan" if math.isnan(mantissa) else ("#inf" if mantissa > 0 else "#-inf")
        else:
            s = str(mantissa)
            if "e" in s:
                # Fold Python's built-in float exponent (e.g. `1e-05`) into `exp` so we emit one exponent.
                s, mExp = s.split("e")
                exp += int(mExp)
            if exp != 0:
                cfg = config or printing.defaults
                s = f"{s}{cfg.exponent}{exp:+}"
        if self.tag is None:
            return s
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)}){s}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class Infinity(Numberish):
    """KDL `#inf` or `#-inf` floating-point keyword value."""

    value: float
    tag: str | None = None

    def __post_init__(self) -> None:
        """Validate that `value` is positive or negative infinity."""
        if not math.isinf(self.value):
            msg = f"Infinity value must be infinite; got {self.value!r}."
            raise ValueError(msg)

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `#inf` or `#-inf`."""
        if not math.isinf(self.value):
            msg = f"Infinity value must be infinite; got {self.value!r}."
            raise ValueError(msg)
        kw = "#inf" if self.value > 0 else "#-inf"
        if self.tag is None:
            return kw
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)}){kw}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class NaN(Numberish):
    """KDL `#nan` floating-point keyword value."""

    value: float = float("nan")
    tag: str | None = None

    def __post_init__(self) -> None:
        """Validate that `value` is NaN."""
        if not math.isnan(self.value):
            msg = f"NaN value must be NaN; got {self.value!r}."
            raise ValueError(msg)

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `#nan`."""
        if self.tag is None:
            return "#nan"
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)})#nan"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class Hex(Numberish):
    """KDL hexadecimal integer literal (`0x...`)."""

    value: int
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `0x...` (when `respectRadix` is True) or decimal."""
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
        return f"({_printStr(self.tag, cfg.respectStringType)}){numStr}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class Bool(Value):
    """KDL boolean keyword value (`#true` or `#false`)."""

    value: bool
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `#true` or `#false`."""
        kw = "#true" if self.value else "#false"
        if self.tag is None:
            return kw
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)}){kw}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class Null(Value):
    """KDL `#null` keyword value."""

    tag: str | None = None

    @property
    @t.override
    def value(self) -> None:
        """Return `None`."""
        return None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as `#null`."""
        if self.tag is None:
            return "#null"
        cfg = config or printing.defaults
        return f"({_printStr(self.tag, cfg.respectStringType)})#null"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


class Stringish(Value):  # pylint: disable=abstract-method
    """Base class for all KDL string value wrappers (`String`, `IdentString`, `RawString`)."""

    __slots__ = ()


_searchRawDisallowed = DISALLOWED_OR_NL_PATTERN.search
_findQuoteHashRuns = re.compile(r'"(#*)').finditer


@dataclass(slots=True)
class RawString(Stringish):
    """KDL raw string literal (`#"..."#`)."""

    value: str
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as a raw `#...#` string when allowed and printable, else quoted/ident."""
        cfg = config or printing.defaults
        strMode = cfg.respectStringType
        tagStr = f"({_printStr(self.tag, strMode)})" if self.tag is not None else ""
        if strMode == "auto" or strMode is True:
            if _isPrintableRawString(self.value):
                hashes = "#" * _findRequiredHashCount(self.value)
                return f'{tagStr}{hashes}"{self.value}"{hashes}'
            return f'{tagStr}"{_escapedFromRaw(self.value)}"'
        if strMode is False:
            body = _IDENT_CACHE.get(self.value) or _printIdent(self.value)
            return f"{tagStr}{body}"
        msg = f"Invalid respectStringType {strMode!r}; expected True, False, or 'auto'."
        raise ValueError(msg)

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


def _isPrintableRawString(chars: str) -> bool:
    """Return True if `chars` can be emitted as a single-line KDL 2.0 raw string."""
    # Leading quotes (e.g. `"` -> `#"""#`), newlines, or disallowed codepoints can't be represented as
    # single-line KDL2 raw strings.
    return not chars.startswith('"') and (chars.isprintable() or _searchRawDisallowed(chars) is None)


def _findRequiredHashCount(chars: str) -> int:
    """Return the minimum `#` delimiter count so `"` + `#*n` never occurs inside `chars`."""
    if '"' not in chars:
        return 1
    # One more `#` than the longest run of `#`s immediately following any `"` prevents early termination.
    return max((len(m.group(1)) for m in _findQuoteHashRuns(chars)), default=0) + 1


@dataclass(slots=True)
class IdentString(Stringish):
    """KDL identifier string literal (bare when valid, quoted otherwise)."""

    value: str
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize according to `config.respectStringType`."""
        cfg = config or printing.defaults
        strMode = cfg.respectStringType
        body = _printStr(self.value, strMode)
        if self.tag is None:
            return body
        return f"({_printStr(self.tag, strMode)}){body}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
        return self.print()


@dataclass(slots=True)
class String(Stringish):
    """KDL quoted string literal."""

    value: str
    tag: str | None = None

    @t.override
    def print(self, config: t.PrintConfig | None = None) -> str:
        """Serialize as quoted (`exact`), bare-or-quoted (`plain`), or cleanest form (`auto`)."""
        cfg = config or printing.defaults
        strMode = cfg.respectStringType
        if strMode == "auto":
            body = _AUTO_STR_CACHE.get(self.value) or _printAutoString(self.value)
        elif strMode is True:
            body = _QUOTED_STR_CACHE.get(self.value) or _printQuotedString(self.value)
        elif strMode is False:
            body = _IDENT_CACHE.get(self.value) or _printIdent(self.value)
        else:
            msg = f"Invalid respectStringType {strMode!r}; expected True, False, or 'auto'."
            raise ValueError(msg)
        if self.tag is None:
            return body
        return f"({_printStr(self.tag, strMode)}){body}"

    def __str__(self) -> str:
        """Serialize this value using the default print configuration."""
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


def _toKdlNode(val: t.Any) -> Node:
    """Return `val` if it is already a `Node`, or convert it via its `.to_kdl()` method."""
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


# O(1) exact-type dispatch for stdlib native types in `_toKdlValue`; subclasses still fall through to isinstance().
_EXACT_STDLIB_TO_KDL: dict[type, t.Callable[[t.Any], t.KDLValue]] = {
    decimal.Decimal: lambda v: String(str(v), "decimal"),
    datetime.datetime: lambda v: String(v.isoformat(), "date-time"),
    datetime.time: lambda v: String(v.isoformat(), "time"),
    datetime.date: lambda v: String(v.isoformat(), "date"),
    ipaddress.IPv4Address: lambda v: String(str(v), "ipv4"),
    ipaddress.IPv6Address: lambda v: String(str(v), "ipv6"),
    urllib.parse.ParseResult: lambda v: String(urllib.parse.urlunparse(v), "url"),
    uuid.UUID: lambda v: String(str(v), "uuid"),
    bytes: lambda v: String(base64.b64encode(v).decode("utf-8"), "base64"),
}


def _toKdlValue(val: t.Any) -> t.KDLValue:
    """
    Converts any KDLish value (a KDLValue, a primitive,
    an object corresponding to a built-in tag,
    or an object with .to_kdl() that returns one of the above)
    into a KDLValue
    """
    exactConv = _EXACT_STDLIB_TO_KDL.get(type(val))
    if exactConv is not None:
        return exactConv(val)
    if isinstance(val, Value):
        return val
    if val is None:
        return Null()
    if isinstance(val, bool):
        return Bool(val)
    if isinstance(val, str):
        return IdentString(val)
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
        # Fast path for the default UNICODE flag; otherwise verify flags come solely from inline `(?...)` flags.
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
    if _isKdlishValue(convertedVal):
        return _toKdlValue(convertedVal)
    msg = f"Expected object to convert to KDL value or compatible primitive. Got:\n{val!r}"
    raise TypeError(msg)


def _isKdlishValue(val: t.Any) -> bool:
    """Return True if `val` is a `Value`, primitive, or supported stdlib type convertible to KDL."""
    if val is None:
        return True
    if isinstance(val, re.Pattern):
        return isinstance(val.pattern, str)
    return isinstance(val, _KDLISH_TYPES)


def _findIndexedNode(nodes: list[Node], tagKey: t.TagKey, idx: int) -> Node:
    """Return the `idx`-th node (supporting negative indices) in `nodes` whose tag matches `tagKey`."""
    if tagKey is Ellipsis:
        return nodes[idx]
    if idx >= 0:
        seen = 0
        for child in nodes:
            kdlNode = child if isinstance(child, Node) else _toKdlNode(child)
            if tagMatchesKey(kdlNode.tag, tagKey):
                if seen == idx:
                    return child
                seen += 1
    else:
        seen = 0
        for child in reversed(nodes):
            kdlNode = child if isinstance(child, Node) else _toKdlNode(child)
            if tagMatchesKey(kdlNode.tag, tagKey):
                seen -= 1
                if seen == idx:
                    return child
    msg = f"Node index {idx} out of range for tag key {tagKey!r}."
    raise IndexError(msg)


def _getNodeByKey(nodes: list[Node], key: t.NodeKey) -> Node:
    """Return the first node in `nodes` matching `key`, raising KeyError/IndexError if absent."""
    if isinstance(key, str):
        nodeCls = Node
        for node in nodes:
            kdlNode = node if isinstance(node, nodeCls) else _toKdlNode(node)
            if kdlNode.name == key:
                return node
        raise KeyError(key)
    if isinstance(key, bool):
        msg = f"Invalid NameKey {key!r}"
        raise TypeError(msg)
    if isinstance(key, int):
        return nodes[key]
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid NodeKey {key!r}"
            raise TypeError(msg)
        tagKey, nameKey = key
        if isinstance(nameKey, bool):
            msg = f"Invalid NameKey {nameKey!r}"
            raise TypeError(msg)
        if isinstance(nameKey, int):
            return _findIndexedNode(nodes, tagKey, nameKey)
    for node in nodes:
        if nodeMatchesKey(node, key):
            return node
    raise KeyError(key)


def _iterNodesByKey(nodes: list[Node], key: t.NodeKey) -> t.Iterable[Node]:
    """Yield every node in `nodes` matching `key`."""
    if isinstance(key, str):
        nodeCls = Node
        for node in nodes:
            kdlNode = node if isinstance(node, nodeCls) else _toKdlNode(node)
            if kdlNode.name == key:
                yield node
        return
    if isinstance(key, bool):
        msg = f"Invalid NameKey {key!r}"
        raise TypeError(msg)
    if isinstance(key, int):
        try:
            node = nodes[key]
        except IndexError:
            return
        yield node
        return
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid NodeKey {key!r}"
            raise TypeError(msg)
        tagKey, nameKey = key
        if isinstance(nameKey, bool):
            msg = f"Invalid NameKey {nameKey!r}"
            raise TypeError(msg)
        if isinstance(nameKey, int):
            try:
                node = _findIndexedNode(nodes, tagKey, nameKey)
            except IndexError:
                return
            yield node
            return
    for node in nodes:
        if nodeMatchesKey(node, key):
            yield node


def nodeMatchesKey(node: t.Any, key: t.NodeKey) -> bool:
    """Return True if `node` matches a `NodeKey` (`nameKey` or `(tagKey, nameKey)`)."""
    if not isinstance(node, Node):
        node = _toKdlNode(node)
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid NodeKey {key!r}"
            raise TypeError(msg)
        tagKey, nameKey = key
        if isinstance(nameKey, int) and not isinstance(nameKey, bool):
            msg = f"Integer NameKey {nameKey!r} is only valid when searching a node list."
            raise TypeError(msg)
        return tagMatchesKey(node.tag, tagKey) and nameMatchesKey(node.name, nameKey)
    return nameMatchesKey(node.name, key)


def valueMatchesKey(value: t.Any, key: t.ValueKey) -> bool:
    """Return True if `value` matches a `ValueKey` (`tagKey` or `(tagKey, typeKey)`)."""
    # Non-Value objects (e.g. converted Python natives) are untagged by definition.
    tag = value.tag if isinstance(value, Value) else None
    if isinstance(key, tuple):
        if len(key) != 2:
            msg = f"Invalid ValueKey {key!r}"
            raise TypeError(msg)
        tagKey, typeKey = key
        return tagMatchesKey(tag, tagKey) and typeMatchesKey(value, typeKey)
    return tagMatchesKey(tag, key)


def tagMatchesKey(val: str | None, key: t.TagKey) -> bool:
    """Return True if tag `val` matches `key` (`...`, `None`, `str`, `re.Pattern`, or predicate)."""
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
    """Return True if node name `val` matches `key` (`...`/`None`, `str`, `re.Pattern`, or predicate)."""
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
    if isinstance(key, int) and not isinstance(key, bool):
        msg = f"Integer NameKey {key!r} is only valid when searching a node list."
        raise TypeError(msg)
    msg = f"Invalid NameKey {key!r}"
    raise TypeError(msg)


def typeMatchesKey(val: t.Any, key: t.TypeKey) -> bool:
    """Return True if `val` is an instance of `key` (or `key` is `...`)."""
    if key is Ellipsis:
        return True
    try:
        return isinstance(val, t.cast("t._ClassInfo", key))
    except TypeError as e:
        msg = f"Invalid TypeKey {key!r}"
        raise TypeError(msg) from e


_UNICODE_ESCAPE_SUB = re.compile(DISALLOWED_OR_NON_ASCII_NL_RE).sub


def _unicodeEscapeChar(m: re.Match[str]) -> str:
    r"""Format a single matched disallowed/non-ASCII-newline codepoint as `\u{hex}`."""
    return f"\\u{{{ord(m.group()):x}}}"


def _escapedFromRaw(chars: str) -> str:
    """Escape `chars` for use inside a KDL 2.0 double-quoted string."""
    # Chained C-level `.replace()` calls outperform `str.translate()` in CPython.
    res = (
        chars.replace("\\", "\\\\")
        .replace('"', '\\"')
        # Forward slashes are not escaped in KDL 2.0.
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    if not res.isprintable():
        res = _UNICODE_ESCAPE_SUB(_unicodeEscapeChar, res)
    return res


# Bounded caches of formatted strings per mode (always non-empty, so callers can use `cache.get(s) or fmt(s)`).
_IDENT_CACHE: dict[str, str] = {}
_AUTO_STR_CACHE: dict[str, str] = {}
_QUOTED_STR_CACHE: dict[str, str] = {}


def _getStrPrinter(mode: t.StringTypeMode) -> tuple[dict[str, str], t.Callable[[str], str]]:
    """Return the `(cache, formatter)` pair for node names, tags, keys, and default strings in `mode`."""
    if mode == "auto":
        return (_AUTO_STR_CACHE, _printAutoString)
    if mode is True or mode is False:
        return (_IDENT_CACHE, _printIdent)
    msg = f"Invalid respectStringType {mode!r}; expected True, False, or 'auto'."
    raise ValueError(msg)


def _printStr(chars: str, mode: t.StringTypeMode) -> str:
    """Format an identifier/string `chars` according to `mode` using the corresponding cache."""
    if mode == "auto":
        return _AUTO_STR_CACHE.get(chars) or _printAutoString(chars)
    if mode is True or mode is False:
        return _IDENT_CACHE.get(chars) or _printIdent(chars)
    msg = f"Invalid respectStringType {mode!r}; expected True, False, or 'auto'."
    raise ValueError(msg)


def _printIdent(chars: str) -> str:
    """Format `chars` as a bare identifier if valid, otherwise as a double-quoted string."""
    cached = _IDENT_CACHE.get(chars)
    if cached is not None:
        return cached
    res = chars if isBareIdent(chars) else f'"{_escapedFromRaw(chars)}"'
    # Only cache short strings and flush at 4096 entries to cap memory on large/unbounded documents.
    if len(chars) <= 64:
        if len(_IDENT_CACHE) >= 4096:
            _IDENT_CACHE.clear()
        _IDENT_CACHE[chars] = res
    return res


def _printQuotedString(chars: str) -> str:
    """Format `chars` unconditionally as a double-quoted KDL string."""
    cached = _QUOTED_STR_CACHE.get(chars)
    if cached is not None:
        return cached
    res = f'"{_escapedFromRaw(chars)}"'
    if len(chars) <= 64:
        if len(_QUOTED_STR_CACHE) >= 4096:
            _QUOTED_STR_CACHE.clear()
        _QUOTED_STR_CACHE[chars] = res
    return res


def _printAutoString(chars: str) -> str:
    """Format `chars` in its cleanest KDL 2.0 form: bare ident, plain quoted, raw `#...#`, or escaped quoted."""
    cached = _AUTO_STR_CACHE.get(chars)
    if cached is not None:
        return cached
    if isBareIdent(chars):
        res = chars
    else:
        escaped = _escapedFromRaw(chars)
        if escaped == chars:
            res = f'"{chars}"'
        elif _isPrintableRawString(chars):
            # Prefer raw syntax when escaping would otherwise be needed (e.g. quotes or backslashes).
            hashes = "#" * _findRequiredHashCount(chars)
            res = f'{hashes}"{chars}"{hashes}'
        else:
            res = f'"{escaped}"'
    if len(chars) <= 64:
        if len(_AUTO_STR_CACHE) >= 4096:
            _AUTO_STR_CACHE.clear()
        _AUTO_STR_CACHE[chars] = res
    return res
