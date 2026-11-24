from __future__ import annotations

import re
import sys
from dataclasses import dataclass

from . import converters, kdl1check, parsing, t
from .chars import (
    BARE_IDENT_PATTERN,
    BARE_IDENT_RE_STR,
    CONFUSABLE_KEYWORDS,
    DIGITS,
    DISALLOWED_RE,
    LINESPACE_CHAR_SET,
    NAN,
    NATIVE_KEYWORDS,
    NEG_INF,
    NL_CHARS,
    NL_RE,
    NL_SET,
    POS_INF,
    WS_CHARS,
    WS_RE,
)
from .errors import ParseError, ParseFragment
from .stream import Stream
from .types import (
    Binary,
    Bool,
    Decimal,
    Document,
    Hex,
    IdentString,
    Infinity,
    NaN,
    Node,
    Null,
    Octal,
    RawString,
    String,
    Value,
    _scaleInt,
)

if t.TYPE_CHECKING:
    from .t import ParseResult

    # (lineStartPos, indent, text)
    type _MultilineLine = tuple[int, str, str]
    # (name, tag, nameEnd, args, props, discard, spaceEnd)
    type _NodeHead = tuple[str, str | None, int, list[t.Any], dict[str, t.Any], bool, int]

# 1-char lookahead sets used to skip unnecessary scanner calls (and dispatch bare idents in parseEntry).
_NODESPACE_START = frozenset(WS_CHARS + "/\\")
_LINESPACE_START = frozenset(WS_CHARS + NL_CHARS + "/\\")
_SPECIAL_ENTRY_START = frozenset('"{};\n\r)/(#0123456789+-')
# ASCII characters guaranteed not to continue a KDL2 bare identifier (used for regex-free keyword termination).
_KW_DELIM = frozenset(' \t\n\r;{}()[]/\\"#=')

# Precompiled scanner patterns and bound matchers
_matchUnicodeSpace = re.compile(rf"[{WS_RE}]+").match
_matchSingleLineComment = re.compile(rf"//[^{NL_RE}{DISALLOWED_RE}]*(?:\r\n|[{NL_RE}])?").match
_matchLinespaceChars = re.compile(rf"[{WS_RE}{NL_RE}]+").match
_searchBlockCommentToken = re.compile(rf"/\*|\*/|[{DISALLOWED_RE}]").search

_matchBareIdent = BARE_IDENT_PATTERN.match
_matchPlainQuoteChunk = re.compile(rf'[^"\\{NL_RE}{DISALLOWED_RE}]*').match
_matchRawQuoteChunk = re.compile(rf'[^"{NL_RE}{DISALLOWED_RE}]*').match

# Captures bare ident (group 1) and optionally `=value` for common untagged property values:
# group 2 = plain quoted string body, group 3 = decimal literal, group 4 = lowercase #keyword name.
_PLAIN_QUOTE_RE_STR = rf'"([^"\\{NL_RE}{DISALLOWED_RE}]*)"(?![#"])'
_SIMPLE_DEC_RE_STR = r"[+-]?[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?(?![.eExob])"
_SIMPLE_KW_RE_STR = r"#(true|false|null|inf|-inf|nan)(?=[ \t\n\r;{}()\[\]/\\\"#=]|$)"
_matchIdentOrSimpleProp = re.compile(
    rf"({BARE_IDENT_RE_STR})(?:=(?:{_PLAIN_QUOTE_RE_STR}|({_SIMPLE_DEC_RE_STR})|{_SIMPLE_KW_RE_STR}))?",
).match

# Optional groups let `lastindex` distinguish plain int (None), plain float (1), and scinot (2).
_matchDecimal = re.compile(r"[+-]?[0-9][0-9_]*(\.[0-9][0-9_]*)?(?:[eE]([+-]?[0-9][0-9_]*))?").match
_matchDecDigits = re.compile(r"[0-9][0-9_]*").match
_matchBinDigits = re.compile(r"[01][01_]*").match
_matchOctDigits = re.compile(r"[0-7][0-7_]*").match
_matchHexDigits = re.compile(r"[0-9a-fA-F][0-9a-fA-F_]*").match
_matchHexEscape = re.compile(r"[0-9a-fA-F]*").match

_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
    "b": "\b",
    "f": "\f",
    "s": " ",
}

# ParseResult convention: `(value, nextIndex)` with `index < 0` for no-match,
# and `(None, index >= 0)` for successfully parsed but discarded (`/-`) items.
_FAIL: ParseResult[None] = (None, -1)
_KDL1_MODULE_NAME = f"{__package__}.kdl1"


def _parseKdl1(input: str, config: t.ParseConfig) -> Document:
    """Lazily load `kdl.kdl1` and parse `input` using the KDL 1.0 parser."""
    # Dispatch through the module object (not a cached function ref) so test monkeypatches on `kdl1.parse` still work.
    kdl1Mod = sys.modules.get(_KDL1_MODULE_NAME)
    if kdl1Mod is None:
        from . import kdl1

        kdl1Mod = kdl1
    return t.cast("Document", kdl1Mod.parse(input, config))


def parse(input: str, config: t.ParseConfig | None = None) -> Document:
    """Parse a KDL string into a `Document`, honoring `config.version` (`1`, `2`, or `'auto'`)."""
    config = config or parsing.defaults
    match config.version:
        case 1:
            return _parseKdl1(input, config)
        case 2:
            allowFallback = False
        case "auto":
            allowFallback = True
        case _:
            msg = f"Invalid KDL version {config.version!r}; expected 1, 2, or 'auto'."
            raise ValueError(msg)
    s = Stream(input, config)
    chars = s.chars
    n = s.length
    # KDL 2.0 allows a single leading UTF-8 BOM before the document.
    i = 1 if chars.startswith("\ufeff") else 0
    if allowFallback and i + 2 <= n and chars[i] == "/" and chars[i + 1] == "-":
        markerVersion = kdl1check.detectVersionMarker(chars, i)
        if markerVersion == 1:
            return _parseKdl1(input, config)
        if markerVersion == 2:
            allowFallback = False

    doc = Document()
    try:
        doc.nodes = _parseNodes(s, i)
    except ParseError as err:
        if not allowFallback or not err.maybeKdl1:
            raise
        try:
            return _parseKdl1(input, config)
        except ParseError:
            # If KDL 1.0 also rejects the input, report the original KDL 2.0 error.
            raise err from None
    return doc


@dataclass(slots=True)
class _OpenNode:
    """Saved ancestor node frame on `_parseNodes`'s explicit stack while parsing its `{ ... }` child block."""

    start: int
    nameEnd: int
    name: str
    tag: str | None
    args: list[t.Any]
    props: dict[str, t.Any]
    children: list[Node]
    hasRealChildren: bool
    nodeDiscard: bool
    blockStart: int
    blockIsReal: bool
    parentNodes: list[Node]
    parentDiscard: bool


def _parseNodeHead(
    s: Stream,
    start: int,
    discard: bool,
) -> _NodeHead | None:
    """Parse one node's optional `/-`, optional `(tag)`, name, and entries, returning None on no-match."""
    chars = s.chars
    n = s.length
    if start >= n:
        return None
    nodespaceStart = _NODESPACE_START

    if chars[start] == "/" and start + 1 < n and chars[start + 1] == "-":
        i = _skipLinespace(s, start + 2)
        discard = True
    else:
        i = start

    tag: str | None = None
    if i < n and chars[i] == "(":
        tag, tagEnd = _parseTag(s, i)
        if tagEnd < 0:
            return None
        i = _skipNodespace(s, tagEnd) if (tagEnd < n and chars[tagEnd] in nodespaceStart) else tagEnd

    if i >= n:
        return None
    if chars[i] not in ('"', "#"):
        mName = _matchBareIdent(chars, i)
        if mName is None:
            return None
        name = mName.group()
        if name in CONFUSABLE_KEYWORDS:
            raise ParseError(
                s,
                i,
                f"Ident strings confusable with keywords aren't allowed; use a quoted string. Got '{name}'.",
                maybeKdl1=name not in kdl1check.KDL1_KEYWORDS,
            )
        nameEnd = mName.end()
    else:
        parsedName, nameEnd = _parseStringText(s, i)
        if parsedName is None:
            return None
        name = parsedName

    args: list[t.Any] = []
    props: dict[str, t.Any] = {}
    spaceEnd = _parseEntries(s, nameEnd, discard, args, props)
    return (name, tag, nameEnd, args, props, discard, spaceEnd)


def _applyNodeConverters(
    s: Stream,
    node: Node,
    start: int,
    nameEnd: int,
) -> Node:
    """Apply the first matching user `nodeConverter` (that does not return `NotImplemented`) to `node`."""
    pf: ParseFragment | None = None
    for convKey, converter in s.config.nodeConverters.items():
        if node.matchesKey(convKey):
            if pf is None:
                pf = ParseFragment(s.chars[start:nameEnd], s, start)
            converted = converter(node, pf)
            if converted == NotImplemented:
                continue
            return t.cast("Node", converted)
    return node


def _parseNodes(s: Stream, start: int) -> list[Node]:
    """Iteratively parse a KDL 2.0 node tree starting at `start` using an explicit block stack."""
    chars = s.chars
    n = s.length
    i = _skipLinespace(s, start) if (start < n and chars[start] in _LINESPACE_START) else start

    rootNodes: list[Node] = []
    curNodes = rootNodes
    curDiscard = False
    # Explicit stack of open ancestor nodes avoids Python recursion limits on deeply nested documents.
    stack: list[_OpenNode] = []
    hasNodeConverters = bool(s.config.nodeConverters)

    while True:
        # 1. Inside a child block, `}` (or EOF) closes the block and resumes its owner node.
        if stack and (i >= n or chars[i] == "}"):
            if i >= n:
                raise ParseError(s, stack[-1].blockStart, "Hit EOF while searching for end of child list")
            frame = stack.pop()
            i += 1
            if frame.blockIsReal:
                frame.children = curNodes
                frame.hasRealChildren = True
            curNodes = frame.parentNodes
            curDiscard = frame.parentDiscard
            spaceEnd = _skipNodespace(s, i) if (i < n and chars[i] in _NODESPACE_START) else i
            nodeStart = frame.start
            nameEnd = frame.nameEnd
            name = frame.name
            tag = frame.tag
            args = frame.args
            props = frame.props
            children = frame.children
            hasRealChildren = frame.hasRealChildren
            nodeDiscard = frame.nodeDiscard
        else:
            # 2. Top-level EOF finishes the document.
            if i >= n:
                break
            # 3. Parse the next node's header (`/-`, `(tag)`, `name`, args, props).
            head = _parseNodeHead(s, i, curDiscard)
            if head is None:
                if stack:
                    raise ParseError(
                        s,
                        i,
                        "Junk between end of child list and closing }",
                        maybeKdl1=kdl1check.looksLikeAfterNode(chars, i, n)
                        or kdl1check.looksLikeNodeStart(chars, i, n),
                    )
                raise ParseError(
                    s,
                    i,
                    "Unexpected non-node content",
                    maybeKdl1=kdl1check.looksLikeNodeStart(chars, i, n),
                )
            nodeStart = i
            name, tag, nameEnd, args, props, nodeDiscard, spaceEnd = head
            children = []
            hasRealChildren = False

        # 4. Enter a child block (`{ ... }` or slashdashed `/- { ... }`) if one follows this node.
        if spaceEnd < n:
            ch = chars[spaceEnd]
            if ch == "{" and not hasRealChildren:
                stack.append(
                    _OpenNode(
                        start=nodeStart,
                        nameEnd=nameEnd,
                        name=name,
                        tag=tag,
                        args=args,
                        props=props,
                        children=children,
                        hasRealChildren=hasRealChildren,
                        nodeDiscard=nodeDiscard,
                        blockStart=spaceEnd,
                        blockIsReal=True,
                        parentNodes=curNodes,
                        parentDiscard=curDiscard,
                    ),
                )
                curNodes = []
                curDiscard = nodeDiscard
                i = spaceEnd + 1
                if i < n and chars[i] in _LINESPACE_START:
                    mLs = _matchLinespaceChars(chars, i)
                    if mLs is not None:
                        i = mLs.end()
                    if i < n and chars[i] in ("/", "\\"):
                        i = _skipLinespace(s, i)
                continue
            if ch == "/" and spaceEnd + 1 < n and chars[spaceEnd + 1] == "-":
                sdEnd = _skipLinespace(s, spaceEnd + 2)
                if sdEnd < n and chars[sdEnd] == "{":
                    stack.append(
                        _OpenNode(
                            start=nodeStart,
                            nameEnd=nameEnd,
                            name=name,
                            tag=tag,
                            args=args,
                            props=props,
                            children=children,
                            hasRealChildren=hasRealChildren,
                            nodeDiscard=nodeDiscard,
                            blockStart=sdEnd,
                            blockIsReal=False,
                            parentNodes=curNodes,
                            parentDiscard=curDiscard,
                        ),
                    )
                    curNodes = []
                    curDiscard = True
                    i = sdEnd + 1
                    if i < n and chars[i] in _LINESPACE_START:
                        i = _skipLinespace(s, i)
                    continue

        # 5. No more child blocks for this node — finalize it and advance past its terminator.
        i = spaceEnd
        if not nodeDiscard:
            node = Node(name, tag, args, props, children)
            if hasNodeConverters:
                node = _applyNodeConverters(s, node, nodeStart, nameEnd)
            curNodes.append(node)

        termEnd = _skipTerminatorAndLinespace(s, i)
        if termEnd < 0:
            if stack:
                if i < n and chars[i] == "}":
                    continue
                raise ParseError(
                    s,
                    i,
                    "Junk between end of child list and closing }",
                    maybeKdl1=kdl1check.looksLikeAfterNode(chars, i, n) or kdl1check.looksLikeNodeStart(chars, i, n),
                )
            got = chars[i] if i < n else ""
            raise ParseError(
                s,
                i,
                f"Expected a node terminator (newline, ;, or EOF). Got '{got}'",
                maybeKdl1=kdl1check.looksLikeAfterNode(chars, i, n),
            )
        i = termEnd

    return rootNodes


def _parseTag(s: Stream, start: int) -> ParseResult[str]:
    """Parse a `(type)` annotation starting at `start`, returning `(tagText, nextIndex)` or `_FAIL`."""
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "(":
        return _FAIL
    i = start + 1
    if i < n and chars[i] in _NODESPACE_START:
        i = _skipNodespace(s, i)
    if i >= n:
        return _FAIL
    if chars[i] not in ('"', "#"):
        mTag = _matchBareIdent(chars, i)
        if mTag is None:
            return _FAIL
        tag: str | None = mTag.group()
        if tag in CONFUSABLE_KEYWORDS:
            raise ParseError(
                s,
                i,
                f"Ident strings confusable with keywords aren't allowed; use a quoted string. Got '{tag}'.",
                maybeKdl1=tag not in kdl1check.KDL1_KEYWORDS,
            )
        i = mTag.end()
    else:
        tag, i = _parseStringText(s, i)
        if tag is None:
            return _FAIL
    if i < n and chars[i] in _NODESPACE_START:
        i = _skipNodespace(s, i)
    if i >= n or chars[i] != ")":
        raise ParseError(
            s,
            i,
            "Junk between tag ident and closing paren.",
            maybeKdl1=kdl1check.looksLikeInTag(chars, i, n),
        )
    return (tag, i + 1)


def _skipTerminatorAndLinespace(s: Stream, start: int) -> int:
    """Consume a node terminator (newline, `;`, `//` comment, or EOF) and trailing linespace, or return -1."""
    # On a newline terminator, `_matchLinespaceChars` consumes blank lines and next-line indent in one regex call.
    chars = s.chars
    n = s.length
    if start >= n:
        return start
    ch = chars[start]
    if ch in NL_SET:
        m = _matchLinespaceChars(chars, start)
        assert m is not None
        i = m.end()
        if i < n and chars[i] in ("/", "\\"):
            return _skipLinespace(s, i)
        return i
    if ch == ";":
        i = start + 1
        return _skipLinespace(s, i) if (i < n and chars[i] in _LINESPACE_START) else i
    if ch == "/" and start + 1 < n and chars[start + 1] == "/":
        return _skipLinespace(s, start)
    return -1


def _makeKeywordValue(ident: str) -> Value:
    """Instantiate the `Value` wrapper for a validated lowercase KDL 2.0 keyword name."""
    match ident:
        case "true":
            return Bool(True)
        case "false":
            return Bool(False)
        case "null":
            return Null()
        case "inf":
            return Infinity(POS_INF)
        case "-inf":
            return Infinity(NEG_INF)
        case _:
            return NaN(NAN)


def _parseIntOrOverflow(raw: str) -> int | float:
    """Parse a decimal integer string, falling back to `float` (e.g. ±inf) if it exceeds `int_max_str_digits`."""
    clean = raw.replace("_", "") if "_" in raw else raw
    try:
        return int(clean, 10)
    except ValueError:
        fval = float(clean)
        # Integer `-000...` with no decimal point has value +0.0 (matching `int("-0")`), not -0.0.
        if fval == 0.0 and clean[0] == "-" and not clean.strip("+-0"):
            return 0.0
        return fval


def _parseDecimalStr(raw: str, native: bool) -> t.Any:
    """Convert a validated KDL decimal literal into a Python `int`/`float` (`native=True`) or `Decimal`."""
    # Fast path for plain unsigned ASCII integers (guaranteed ASCII from caller regexes).
    if raw.isdigit():
        try:
            intVal: int | float = int(raw, 10)
        except ValueError:
            intVal = _parseIntOrOverflow(raw)
        return intVal if native else Decimal(intVal, 0)

    # Other plain integers (signed or underscored, no `.` or `e`/`E`).
    if "." not in raw and "e" not in raw and "E" not in raw:
        try:
            intVal = int(raw, 10)
        except ValueError:
            intVal = _parseIntOrOverflow(raw)
        return intVal if native else Decimal(intVal, 0)

    ePos = raw.find("e")
    if ePos == -1:
        ePos = raw.find("E")
    if ePos == -1:
        # Plain float with decimal point, no exponent.
        try:
            fltVal = float(raw)
        except ValueError:
            fltVal = float(raw.replace("_", ""))
        return fltVal if native else Decimal(fltVal, 0)

    mantStr = raw[:ePos]
    expStr = raw[ePos + 1 :]
    return _parseScinot(raw, mantStr, expStr, native)


def _parseScinot(raw: str, mantStr: str, expStr: str, native: bool) -> t.Any:
    """Convert a scientific-notation decimal (`mantStr` + `e`/`E` + `expStr`) to native or `Decimal`."""
    cleanMant = mantStr.replace("_", "") if "_" in mantStr else mantStr
    cleanExp = expStr.replace("_", "") if "_" in expStr else expStr
    if native:
        if "." not in cleanMant:
            try:
                expInt = int(cleanExp, 10)
                if expInt >= 0:
                    mInt = int(cleanMant, 10)
                    return mInt if expInt == 0 else _scaleInt(mInt, expInt)
            except ValueError:
                pass
        cleanRaw = raw.replace("_", "") if "_" in raw else raw
        fval = float(cleanRaw)
        # Match Decimal(int("-0"), -k).value (+0.0) while preserving negative zero for `-0.0` and underflows.
        if fval == 0.0 and cleanRaw[0] == "-" and "." not in cleanMant and not cleanMant.strip("+-0"):
            return 0.0
        return fval

    try:
        mantissa: int | float = float(cleanMant) if "." in cleanMant else int(cleanMant, 10)
        exponent = int(cleanExp, 10)
        return Decimal(mantissa, exponent)
    except ValueError:
        cleanRaw = raw.replace("_", "") if "_" in raw else raw
        fval = float(cleanRaw)
        if fval == 0.0 and cleanRaw[0] == "-" and "." not in cleanMant and not cleanMant.strip("+-0"):
            fval = 0.0
        return Decimal(fval, 0)


def _parseEntries(
    s: Stream,
    i: int,
    discard: bool,
    args: list[t.Any],
    props: dict[str, t.Any],
) -> int:
    """Parse all arguments and properties following a node name, returning the offset after trailing nodespace."""
    chars = s.chars
    n = s.length
    fast = s.fastUntagged
    hasValConv = bool(s.config.valueConverters)
    nodespaceStart = _NODESPACE_START
    specialStart = _SPECIAL_ENTRY_START
    confusable = CONFUSABLE_KEYWORDS
    matchIdentOrProp = _matchIdentOrSimpleProp
    argsAppend = args.append

    while True:
        if i >= n:
            return i
        sepCh = chars[i]
        # Inline fast path for the common single-space separator to avoid calling _skipNodespace().
        if sepCh == " ":
            spaceEnd = i + 1
            if spaceEnd < n and chars[spaceEnd] in nodespaceStart:
                spaceEnd = _skipNodespace(s, spaceEnd)
        elif sepCh in nodespaceStart:
            spaceEnd = _skipNodespace(s, i)
        else:
            return i
        if spaceEnd >= n:
            return spaceEnd

        ch = chars[spaceEnd]
        # Fast exit when nodespace lands on a child block or common node terminator.
        if ch in "{};\n\r":
            return spaceEnd
        if ch == "/" and spaceEnd + 1 < n and chars[spaceEnd + 1] == "-":
            entryStart = _skipLinespace(s, spaceEnd + 2)
            entryDiscard = True
        elif spaceEnd == i:
            return spaceEnd
        else:
            entryStart = spaceEnd
            entryDiscard = discard

        if entryStart >= n:
            return spaceEnd
        ech = chars[entryStart]

        if ech not in specialStart:
            # Fast path: bare identifier, optionally followed immediately by `=simpleValue`
            # (plain quoted string, decimal number, or lowercase #keyword) in one regex step.
            m = matchIdentOrProp(chars, entryStart)
            if m is None:
                return spaceEnd
            li = m.lastindex
            text = m.group(1)
            end = m.end()
            if text in confusable:
                isPropKey = (li is not None and li > 1) or (end < n and chars[end] == "=")
                raise ParseError(
                    s,
                    entryStart,
                    f"Ident strings confusable with keywords aren't allowed; use a quoted string. Got '{text}'.",
                    maybeKdl1=(not isPropKey) if text in kdl1check.KDL1_KEYWORDS else isPropKey,
                )
            # `li == 1` means only the bare ident matched (arg or complex property); `li > 1` matched `key=simpleValue`.
            if li != 1:
                i = end
                if entryDiscard:
                    continue
                if li == 2:
                    # Group 2: plain single-line quoted string value (`key="..."`).
                    sVal = m.group(2)
                    if fast:
                        props[text] = sVal
                    elif hasValConv:
                        props[text] = _finalizeValue(s, String(sVal), None, m.start(2) - 1, end)
                    else:
                        props[text] = String(sVal)
                elif li == 3:
                    # Group 3: decimal number value (`key=123`, `key=-1.5e2`, etc.).
                    numStr = m.group(3)
                    if fast and numStr.isdigit():
                        try:
                            props[text] = int(numStr, 10)
                        except ValueError:
                            props[text] = _parseIntOrOverflow(numStr)
                    else:
                        numVal = _parseDecimalStr(numStr, native=fast)
                        if hasValConv:
                            props[text] = _finalizeValue(s, t.cast("Value", numVal), None, m.start(3), end)
                        else:
                            props[text] = numVal
                else:
                    # Group 4: lowercase keyword value (`key=#true`, `#false`, `#null`, `#inf`, `#-inf`, `#nan`).
                    kwName = m.group(4)
                    if fast:
                        props[text] = NATIVE_KEYWORDS[kwName]
                    elif hasValConv:
                        props[text] = _finalizeValue(s, _makeKeywordValue(kwName), None, m.start(4) - 1, end)
                    else:
                        props[text] = _makeKeywordValue(kwName)
                continue
            strEnd = end
        elif ech == '"':
            textOrNone, strEnd = _parseStringText(s, entryStart)
            assert textOrNone is not None
            text = textOrNone
        elif ech in DIGITS or (ech in "+-" and entryStart + 1 < n and chars[entryStart + 1] in DIGITS):
            if entryDiscard or fast:
                val, valEnd = _parseNumber(s, entryStart, native=True)
                i = valEnd
                if not entryDiscard:
                    argsAppend(val)
            else:
                numVal, valEnd = _parseNumber(s, entryStart, native=False)
                i = valEnd
                argsAppend(
                    _finalizeValue(s, t.cast("Value", numVal), None, entryStart, valEnd) if hasValConv else numVal,
                )
            continue
        elif ech == "#":
            if entryStart + 1 < n and chars[entryStart + 1] not in ('"', "#"):
                rawFast = entryDiscard or fast
                kwVal, kwEnd = _parseKeyword(s, entryStart, native=rawFast)
                if kwEnd < 0:
                    return spaceEnd
                i = kwEnd
                if not entryDiscard:
                    if rawFast or not hasValConv:
                        argsAppend(kwVal)
                    else:
                        argsAppend(_finalizeValue(s, t.cast("Value", kwVal), None, entryStart, kwEnd))
                continue
            textOrNone, strEnd = _parseStringText(s, entryStart)
            if textOrNone is None:
                return spaceEnd
            text = textOrNone
        elif ech == "(":
            val, valEnd = _parseValue(s, entryStart, discard=entryDiscard)
            if valEnd < 0:
                return spaceEnd
            i = valEnd
            if not entryDiscard:
                argsAppend(val)
            continue
        elif ech in "+-":
            textOrNone, strEnd = _parseStringText(s, entryStart, inEntry=True)
            if textOrNone is None:
                return spaceEnd
            text = textOrNone
        else:
            return spaceEnd

        # Peek past optional nodespace for `=` to distinguish `key=val` properties from string args.
        eqPos = -1
        if strEnd < n:
            nextCh = chars[strEnd]
            if nextCh == "=":
                eqPos = strEnd
            elif nextCh == " " and strEnd + 1 < n and chars[strEnd + 1] not in nodespaceStart:
                if chars[strEnd + 1] == "=":
                    eqPos = strEnd + 1
            elif nextCh in nodespaceStart:
                afterKey = _skipNodespace(s, strEnd)
                if afterKey < n and chars[afterKey] == "=":
                    eqPos = afterKey

        if eqPos >= 0:
            valStart = eqPos + 1
            if valStart < n and chars[valStart] in nodespaceStart:
                valStart = _skipNodespace(s, valStart)
            propVal, propEnd = _parseValue(s, valStart, discard=entryDiscard)
            if propEnd < 0:
                raise ParseError(
                    s,
                    valStart,
                    "Expected value after prop=.",
                    maybeKdl1=kdl1check.isTagStartAt(chars, valStart, n),
                )
            i = propEnd
            if not entryDiscard:
                props[text] = propVal
            continue

        i = strEnd
        if entryDiscard:
            continue
        if fast:
            argsAppend(text)
        else:
            strObj: Value = RawString(text) if ech == "#" else (String(text) if ech == '"' else IdentString(text))
            argsAppend(_finalizeValue(s, strObj, None, entryStart, strEnd) if hasValConv else strObj)


def _parseValue(s: Stream, start: int, discard: bool = False) -> ParseResult[t.Any]:
    """Parse a single KDL 2.0 value with optional leading `(tag)`, returning `(value, nextIndex)` or `_FAIL`."""
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    tag: str | None = None
    i = start
    if chars[i] == "(":
        tag, tagEnd = _parseTag(s, i)
        if tagEnd >= 0:
            i = _skipNodespace(s, tagEnd) if (tagEnd < n and chars[tagEnd] in _NODESPACE_START) else tagEnd

    if i >= n:
        if tag is not None:
            raise ParseError(s, i, "Found a tag, but no value following it.")
        return _FAIL

    valueStart = i
    ch = chars[i]
    native = discard or (tag is None and s.fastUntagged)

    if ch in DIGITS or (ch in "+-" and i + 1 < n and chars[i + 1] in DIGITS):
        val, end = _parseNumber(s, i, native=native)
        if discard:
            return (None, end)
        return (val, end) if native else (_finalizeValue(s, t.cast("Value", val), tag, valueStart, end), end)

    if ch == "#" and i + 1 < n and chars[i + 1] not in ('"', "#"):
        val, end = _parseKeyword(s, i, native=native)
        if end < 0:
            if tag is not None:
                raise ParseError(s, i, "Found a tag, but no value following it.")
            return _FAIL
        if discard:
            return (None, end)
        return (val, end) if native else (_finalizeValue(s, t.cast("Value", val), tag, valueStart, end), end)

    text, end = _parseStringText(s, i, inValue=True)
    if text is not None:
        if discard:
            return (None, end)
        if native:
            return (text, end)
        valObj: Value = RawString(text) if ch == "#" else (String(text) if ch == '"' else IdentString(text))
        return (_finalizeValue(s, valObj, tag, valueStart, end), end)

    if tag is not None:
        raise ParseError(s, i, "Found a tag, but no value following it.")
    return _FAIL


def _finalizeValue(
    s: Stream,
    val: Value,
    tag: str | None,
    valueStart: int,
    valueEnd: int,
) -> t.Any:
    """Attach `tag`, apply custom `valueConverters`, and unwrap or convert to native types per `s.config`."""
    val.tag = tag
    config = s.config
    valueConverters = config.valueConverters
    if valueConverters:
        pf: ParseFragment | None = None
        for key, converter in valueConverters.items():
            if val.matchesKey(key):
                if pf is None:
                    pf = ParseFragment(s.chars[valueStart:valueEnd], s, valueStart)
                converted = converter(val, pf)
                if converted == NotImplemented:
                    continue
                return converted

    if tag is None:
        if config.nativeUntaggedValues:
            return val.value
        return val

    if config.nativeTaggedValues and tag in converters.NATIVE_TAGS:
        return converters.toNative(val, s, valueStart, valueEnd)
    return val


def _parseNumber(s: Stream, start: int, native: bool = False) -> ParseResult[t.Any]:
    """Parse a KDL 2.0 decimal or radix (`0x`/`0o`/`0b`) numeric literal starting at `start`."""
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    ch = chars[start]
    if ch in "+-":
        sign = -1 if ch == "-" else 1
        i = start + 1
        if i >= n or chars[i] not in DIGITS:
            return _FAIL
        firstDigit = chars[i]
    elif ch in DIGITS:
        sign = 1
        i = start
        firstDigit = ch
    else:
        return _FAIL

    if firstDigit == "0" and i + 1 < n:
        prefix = chars[i + 1]
        if prefix == "x":
            m = _matchHexDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected hex digit after 0x, got junk.")
            raw = m.group()
            try:
                intVal = int(raw, 16) * sign
            except ValueError:
                intVal = int(raw.replace("_", ""), 16) * sign
            return (intVal if native else Hex(intVal), m.end())
        if prefix == "o":
            m = _matchOctDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected octal digit after 0o, got junk.")
            raw = m.group()
            try:
                intVal = int(raw, 8) * sign
            except ValueError:
                intVal = int(raw.replace("_", ""), 8) * sign
            return (intVal if native else Octal(intVal), m.end())
        if prefix == "b":
            m = _matchBinDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected binary digit after 0b, got junk.")
            raw = m.group()
            try:
                intVal = int(raw, 2) * sign
            except ValueError:
                intVal = int(raw.replace("_", ""), 2) * sign
            return (intVal if native else Binary(intVal), m.end())

    mDec = _matchDecimal(chars, start)
    assert mDec is not None
    end = mDec.end()
    # `_matchDecimal` stops before malformed suffixes like `1.` or `1e`; diagnose the exact error lazily.
    if end < n and chars[end] in ".eE":
        _raiseDecimalError(s, i)

    raw = mDec.group()
    li = mDec.lastindex
    if li is None:
        # Plain integer (no decimal point or exponent).
        try:
            numVal: int | float = int(raw, 10)
        except ValueError:
            numVal = _parseIntOrOverflow(raw)
        return (numVal if native else Decimal(numVal, 0), end)
    if li == 1:
        # Plain float (decimal point, no exponent).
        try:
            fltVal = float(raw)
        except ValueError:
            fltVal = float(raw.replace("_", ""))
        return (fltVal if native else Decimal(fltVal, 0), end)

    # Scientific notation: `mDec.group(2)` is the exponent digits after `e`/`E`.
    expStr = mDec.group(2)
    mantStr = raw[: len(raw) - len(expStr) - 1]
    return (_parseScinot(raw, mantStr, expStr, native), end)


def _raiseDecimalError(s: Stream, digitStart: int) -> t.NoReturn:
    """Re-scan a malformed decimal starting at `digitStart` to raise a precise `ParseError`."""
    chars = s.chars
    n = s.length
    mInt = _matchDecDigits(chars, digitStart)
    assert mInt is not None
    i = mInt.end()
    if i < n and chars[i] == ".":
        mFrac = _matchDecDigits(chars, i + 1)
        if mFrac is None:
            raise ParseError(s, i + 1, "Expected digit after decimal point.")
        i = mFrac.end()
    if i < n and chars[i] in "eE":
        expDigits = i + 2 if (i + 1 < n and chars[i + 1] in "+-") else i + 1
        raise ParseError(s, expDigits, "Expected number after exponent.")
    raise ParseError(s, i, "Expected a node terminator (newline, ;, or EOF).")


def _parseKeyword(s: Stream, start: int, native: bool = False) -> ParseResult[t.Any]:
    """Parse a `#`-prefixed KDL 2.0 keyword (`#true`, `#false`, `#null`, `#inf`, `#-inf`, `#nan`)."""
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "#":
        return _FAIL
    # Fast prefix match for the 6 lowercase KDL2 keywords followed by EOF or an ASCII delimiter.
    kwStart = start + 1
    if kwStart < n:
        c1 = chars[kwStart]
        if c1 == "t" and chars.startswith("true", kwStart):
            end = kwStart + 4
            if end >= n or chars[end] in _KW_DELIM:
                return (True if native else Bool(True), end)
        elif c1 == "f" and chars.startswith("false", kwStart):
            end = kwStart + 5
            if end >= n or chars[end] in _KW_DELIM:
                return (False if native else Bool(False), end)
        elif c1 == "n":
            if chars.startswith("null", kwStart):
                end = kwStart + 4
                if end >= n or chars[end] in _KW_DELIM:
                    return (None if native else Null(), end)
            elif chars.startswith("nan", kwStart):
                end = kwStart + 3
                if end >= n or chars[end] in _KW_DELIM:
                    return (NAN if native else NaN(NAN), end)
        elif c1 == "i" and chars.startswith("inf", kwStart):
            end = kwStart + 3
            if end >= n or chars[end] in _KW_DELIM:
                return (POS_INF if native else Infinity(POS_INF), end)
        elif c1 == "-" and chars.startswith("-inf", kwStart):
            end = kwStart + 4
            if end >= n or chars[end] in _KW_DELIM:
                return (NEG_INF if native else Infinity(NEG_INF), end)

    m = _matchBareIdent(chars, kwStart)
    if m is None:
        return _FAIL
    ident = m.group()
    end = m.end()
    if native:
        val = NATIVE_KEYWORDS.get(ident, t.UNSET)
        if val is not t.UNSET:
            return (val, end)
    elif ident in NATIVE_KEYWORDS:
        return (_makeKeywordValue(ident), end)

    # In KDL 1.0, `#...` is a valid bare identifier so `#foo=1` / `#True=1` may trigger KDL1 fallback.
    isKdl1PropKey = end < n and chars[end] == "="
    if ident in CONFUSABLE_KEYWORDS:
        raise ParseError(s, start, f"KDL keywords must be written in lowercase, got #{ident}", maybeKdl1=isKdl1PropKey)
    raise ParseError(s, start, f"Unknown keyword #{ident}", maybeKdl1=isKdl1PropKey)


def _parseStringText(s: Stream, start: int, *, inValue: bool = False, inEntry: bool = False) -> ParseResult[str]:
    """Parse a KDL 2.0 bare identifier, quoted string, raw string, or multiline string at `start`."""
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    ch = chars[start]
    if ch not in ('"', "#"):
        m = _matchBareIdent(chars, start)
        if m is None:
            return _FAIL
        end = m.end()
        ident = m.group()
        if ident in CONFUSABLE_KEYWORDS:
            # KDL1 allows `true`/`false`/`null` as values, and allows other confusables (e.g. `inf`, `True`) as keys/names.
            if inValue:
                maybeKdl1 = ident in kdl1check.KDL1_KEYWORDS
            elif inEntry:
                maybeKdl1 = ident not in kdl1check.KDL1_KEYWORDS and end < n and chars[end] == "="
            else:
                maybeKdl1 = ident not in kdl1check.KDL1_KEYWORDS
            raise ParseError(
                s,
                start,
                f"Ident strings confusable with keywords aren't allowed; use a quoted string. Got '{ident}'.",
                maybeKdl1=maybeKdl1,
            )
        return (ident, end)

    if ch == '"':
        strStart = start + 1
        if strStart < n and chars[strStart] != '"':
            # Fast path for short, plain, single-line strings; longer, escaped, tab, newline, or disallowed-char
            # strings fall through (`isprintable()` is False for every KDL newline and disallowed codepoint).
            quotePos = chars.find('"', strStart, strStart + 256)
            if quotePos != -1:
                end = quotePos + 1
                if end >= n or chars[end] not in ('"', "#"):
                    sub = chars[strStart:quotePos]
                    if "\\" not in sub and sub.isprintable():
                        return (sub, end)
            return _parseQuotedString(s, strStart, 0)
        quoteStart = start
        hashCount = 0
    else:
        i = start + 1
        while i < n and chars[i] == "#":
            i += 1
        if i >= n or chars[i] != '"':
            return _FAIL
        quoteStart = i
        hashCount = i - start

    i = quoteStart + 1
    while i < n and chars[i] == '"':
        i += 1
    quoteCount = i - quoteStart

    if quoteCount == 1:
        return _parseQuotedString(s, i, hashCount)
    if quoteCount == 2:
        # Empty single-line string `""` / `#""#`: back up 1 so _parseQuotedString sees the closing `"`
        # and validates any trailing `#` or extra quotes.
        return _parseQuotedString(s, i - 1, hashCount)
    if quoteCount == 3:
        return _parseMultilineString(s, i, hashCount)
    raise ParseError(s, start, f"Encountered {quoteCount} quotes in a row.")


def _checkPlainQuoteEnd(s: Stream, start: int, chars: str, end: int, n: int) -> None:
    """Reject trailing `#` or extra `"` characters immediately following a non-raw quoted string."""
    if end < n:
        nextCh = chars[end]
        if nextCh == "#":
            raise ParseError(s, start, "Saw # characters at the end of a non-raw string.")
        if nextCh == '"':
            raise ParseError(s, start, "Single-quote string was ended with multiple quote chars.")


def _parseQuotedString(s: Stream, start: int, hashCount: int) -> ParseResult[str]:
    """Parse the body and closing delimiter of a single-line quoted (`hashCount == 0`) or raw string."""
    chars = s.chars
    n = s.length

    if hashCount == 0:
        matchChunk = _matchPlainQuoteChunk
        m = matchChunk(chars, start)
        assert m is not None
        i = m.end()
        if i < n and chars[i] == '"':
            end = i + 1
            _checkPlainQuoteEnd(s, start, chars, end, n)
            return (chars[start:i], end)

        simpleGet = _SIMPLE_ESCAPES.get
        parts: list[str] = [chars[start:i]] if i > start else []
        append = parts.append
        while True:
            if i >= n:
                raise ParseError(s, start, "Hit EOF while looking for the end of the string")
            ch = chars[i]
            if ch == '"':
                end = i + 1
                _checkPlainQuoteEnd(s, start, chars, end, n)
                return ("".join(parts), end)
            if ch == "\\":
                if i + 1 < n:
                    simple = simpleGet(chars[i + 1])
                    if simple is not None:
                        append(simple)
                        i += 2
                    else:
                        esc, i = _parseEscape(s, i)
                        if esc:
                            append(esc)
                else:
                    esc, i = _parseEscape(s, i)
                    if esc:
                        append(esc)
                m = matchChunk(chars, i)
                assert m is not None
                chunkEnd = m.end()
                if chunkEnd > i:
                    append(chars[i:chunkEnd])
                    i = chunkEnd
            elif ch in NL_SET:
                raise ParseError(
                    s,
                    start,
                    "Saw an unescaped newline in a single-quote string.",
                    maybeKdl1=chars.find('"', i + 1) != -1,
                )
            else:
                raise ParseError(
                    s,
                    i,
                    "Disallowed literal character in string.",
                    maybeKdl1=ch == "\ufeff",
                )

    # Fast path for short, printable single-line raw strings (`#"..."#`).
    # `find(delim)` locates the leftmost `"` followed by >= hashCount `#`s; checking `chars[end] != "#"`
    # ensures an exact hash match, and `isprintable()` rejects all newlines and disallowed codepoints.
    delim = '"' + ("#" * hashCount)
    pos = chars.find(delim, start, start + 256)
    if pos != -1:
        end = pos + hashCount + 1
        if end >= n or chars[end] != "#":
            sub = chars[start:pos]
            if sub.isprintable():
                return (sub, end)

    # General raw single-line string (`#"..."#`)
    i = start
    while True:
        m = _matchRawQuoteChunk(chars, i)
        assert m is not None
        i = m.end()
        if i >= n:
            raise ParseError(s, start, "Hit EOF while looking for the end of the string")
        if chars[i] != '"':
            if chars[i] in NL_SET:
                raise ParseError(s, start, "Saw an unescaped newline in a single-quote string.")
            raise ParseError(
                s,
                i,
                "Disallowed literal character in raw string.",
                maybeKdl1=chars[i] == "\ufeff",
            )
        quotePos = i
        i += 1
        hashEnd = i
        while hashEnd < n and chars[hashEnd] == "#":
            hashEnd += 1
        endingHashCount = hashEnd - i
        if endingHashCount < hashCount:
            i = hashEnd
            continue
        if endingHashCount > hashCount:
            raise ParseError(
                s,
                start,
                f"Expected {hashCount} # chars at end of raw string; got {endingHashCount}.",
            )
        return (chars[start:quotePos], hashEnd)


def _parseEscape(s: Stream, start: int) -> ParseResult[str]:
    r"""Parse a single KDL 2.0 escape sequence (simple, `\u{...}`, or whitespace escape) starting at `\`."""
    chars = s.chars
    n = s.length
    if start + 1 >= n:
        raise ParseError(s, start, "Invalid character escape")
    ch = chars[start + 1]
    simple = _SIMPLE_ESCAPES.get(ch)
    if simple is not None:
        return (simple, start + 2)
    if ch == "u":
        if start + 2 >= n or chars[start + 2] != "{":
            raise ParseError(
                s,
                start,
                "Unicode escapes must surround their codepoint in {}",
            )
        hexStart = start + 3
        m = _matchHexEscape(chars, hexStart)
        assert m is not None
        i = m.end()
        hexCount = i - hexStart
        if i >= n or chars[i] != "}":
            raise ParseError(s, hexStart, "Expected } to finish a unicode escape")
        if hexCount < 1:
            raise ParseError(s, hexStart, "Unicode escape doesn't contain a codepoint")
        if hexCount > 6:
            raise ParseError(
                s,
                hexStart,
                "Unicode escapes can contain at most six digits",
            )
        hexValue = int(chars[hexStart:i], 16)
        if 0xD800 <= hexValue <= 0xDFFF:
            raise ParseError(s, hexStart, "Unicode escapes can't encode surrogate codepoints (U+D800-DFFF)")
        if hexValue > 0x10FFFF:
            raise ParseError(
                s,
                hexStart,
                "Maximum codepoint in a unicode escape is 0x10ffff",
            )
        return (chr(hexValue), i + 1)
    if ch in LINESPACE_CHAR_SET:
        # KDL 2.0 whitespace escape (`\` + one or more whitespace/newline chars) collapses to empty string.
        m = _matchLinespaceChars(chars, start + 1)
        assert m is not None
        return ("", m.end())
    raise ParseError(s, start, "Invalid character escape", maybeKdl1=ch == "/")


def _parseMultilineString(s: Stream, start: int, hashCount: int) -> ParseResult[str]:
    """Parse a KDL 2.0 triple-quoted (or raw `#\"\"\"...\"\"\"#`) multiline string starting after opening quotes."""
    chars = s.chars
    n = s.length
    i = _skipNewline(chars, start, n)
    if i == start:
        raise ParseError(s, start, "Multiline strings must have a newline immediately after their opening quotes.")

    lines: list[_MultilineLine] = []
    lineStart = i
    mSpace = _matchUnicodeSpace(chars, i)
    if mSpace is not None:
        spaceEnd = mSpace.end()
        lineIndent = chars[i:spaceEnd]
        i = spaceEnd
    else:
        lineIndent = ""
    lineParts: list[str] = []
    matchChunk = _matchPlainQuoteChunk if hashCount == 0 else _matchRawQuoteChunk

    while True:
        m = matchChunk(chars, i)
        assert m is not None
        chunkEnd = m.end()
        if chunkEnd > i:
            lineParts.append(chars[i:chunkEnd])
            i = chunkEnd

        if i >= n:
            raise ParseError(s, start, "Hit EOF while looking for the end of the string")

        ch = chars[i]
        if ch == '"':
            quoteStart = i
            i += 1
            while i < n and chars[i] == '"':
                i += 1
            quoteCount = i - quoteStart
            if quoteCount < 3:
                lineParts.append(chars[quoteStart:i])
                continue

            if hashCount == 0:
                if quoteCount > 3:
                    raise ParseError(s, quoteStart, f"Saw {quoteCount} consecutive quotes in a multi-line string.")
                if i < n and chars[i] == "#":
                    raise ParseError(s, i, "Saw # characters at the end of a non-raw string.")
                lastText = lineParts[0] if len(lineParts) == 1 else "".join(lineParts)
                rawChars = _processMultiline(lines, lineStart, lineIndent, lastText, s)
                return (rawChars, i)

            if i >= n or chars[i] != "#":
                lineParts.append(chars[quoteStart:i])
                continue

            hashEnd = i + 1
            while hashEnd < n and chars[hashEnd] == "#":
                hashEnd += 1
            endingHashCount = hashEnd - i
            if endingHashCount < hashCount:
                lineParts.append(chars[quoteStart:hashEnd])
                i = hashEnd
                continue
            if endingHashCount > hashCount:
                raise ParseError(
                    s,
                    start,
                    f"Expected {hashCount} # chars at end of raw multiline string; got {endingHashCount}.",
                )
            if quoteCount > 3:
                # Extra quotes before the closing `"""#...` are content on the closing line,
                # which _processMultiline() will reject as non-whitespace on the final line.
                lineParts.append('"' * (quoteCount - 3))
            lastText = lineParts[0] if len(lineParts) == 1 else "".join(lineParts)
            rawChars = _processMultiline(lines, lineStart, lineIndent, lastText, s)
            return (rawChars, hashEnd)

        if ch == "\\":
            esc, i = _parseEscape(s, i)
            if esc:
                lineParts.append(esc)
            continue

        # Must be a newline or disallowed literal character
        nlEnd = _skipNewline(chars, i, n)
        if nlEnd == i:
            raise ParseError(
                s,
                i,
                "Disallowed literal character in multiline string.",
                maybeKdl1=ch == "\ufeff",
            )
        lineText = lineParts[0] if len(lineParts) == 1 else "".join(lineParts)
        lines.append((lineStart, lineIndent, lineText))
        i = nlEnd
        lineStart = i
        mSpace = _matchUnicodeSpace(chars, i)
        if mSpace is not None:
            spaceEnd = mSpace.end()
            lineIndent = chars[i:spaceEnd]
            i = spaceEnd
        else:
            lineIndent = ""
        lineParts = []


def _processMultiline(
    lines: list[_MultilineLine],
    lastLinePos: int,
    lastIndent: str,
    lastText: str,
    s: Stream,
) -> str:
    """Validate the closing line of a multiline string and strip `lastIndent` from every non-blank line."""
    if lastText:
        raise ParseError(s, lastLinePos, "Multiline string ended with non-whitespace content on last line.")
    prefixLen = len(lastIndent)
    outLines: list[str] = []
    for linePos, indent, text in lines:
        # Only-WS lines don't contribute any characters,
        # just the presence of their line.
        if not text:
            outLines.append("")
        elif prefixLen == 0:
            outLines.append(indent + text)
        elif indent.startswith(lastIndent):
            outLines.append(indent[prefixLen:] + text)
        else:
            raise ParseError(
                s,
                linePos,
                "Multiline string line doesn't start with the same whitespace prefix as the final line.",
            )
    return "\n".join(outLines)


def _skipNewline(chars: str, start: int, n: int) -> int:
    """Advance past a single KDL 2.0 newline (`\\r\\n` counts as one), or return `start` unchanged."""
    if start >= n:
        return start
    ch = chars[start]
    if ch == "\n":
        return start + 1
    if ch == "\r":
        if start + 1 < n and chars[start + 1] == "\n":
            return start + 2
        return start + 1
    if ch in NL_SET:
        return start + 1
    return start


def _skipLinespace(s: Stream, start: int) -> int:
    """Skip zero or more KDL 2.0 whitespace, newlines, comments (`//` and `/* */`), and line continuations."""
    chars = s.chars
    n = s.length
    i = start
    while i < n:
        m = _matchLinespaceChars(chars, i)
        if m is not None:
            i = m.end()
            if i >= n:
                break
        ch = chars[i]
        if ch == "/" and i + 1 < n:
            nextCh = chars[i + 1]
            if nextCh == "/":
                mComment = _matchSingleLineComment(chars, i)
                assert mComment is not None
                i = mComment.end()
                if i < n and chars[i - 1] not in NL_SET:
                    raise ParseError(
                        s,
                        i,
                        "Disallowed literal character in comment.",
                        maybeKdl1=chars[i] == "\ufeff",
                    )
                continue
            if nextCh == "*":
                i = _skipBlockComment(s, i)
                continue
            break
        if ch == "\\":
            nextI = _skipEscline(s, i)
            if nextI == i:
                break
            i = nextI
        else:
            break
    return i


def _skipNodespace(s: Stream, start: int) -> int:
    """Skip intra-node whitespace, block comments, and `\\` line continuations starting at `start`."""
    chars = s.chars
    n = s.length
    if start >= n:
        return start
    ch = chars[start]
    if ch in (" ", "\t"):
        i = start + 1
        if i >= n or chars[i] not in _NODESPACE_START:
            return i
    elif ch not in _NODESPACE_START:
        return start
    else:
        i = start

    while True:
        i = _skipWhitespace(s, i)
        nextI = _skipEscline(s, i)
        if nextI == i:
            return i
        i = nextI


def _skipEscline(s: Stream, start: int) -> int:
    """Skip a `\\` line continuation (`\\` + optional whitespace + newline, `//` comment, or EOF), or return `start`."""
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "\\":
        return start
    i = _skipWhitespace(s, start + 1)
    if i >= n:
        return i
    nlEnd = _skipNewline(chars, i, n)
    if nlEnd > i:
        return nlEnd
    if chars[i] == "/" and i + 1 < n and chars[i + 1] == "/":
        m = _matchSingleLineComment(chars, i)
        assert m is not None
        end = m.end()
        if end < n and chars[end - 1] not in NL_SET:
            raise ParseError(
                s,
                end,
                "Disallowed literal character in comment.",
                maybeKdl1=chars[end] == "\ufeff",
            )
        return end
    return start


def _skipWhitespace(s: Stream, start: int) -> int:
    """Skip horizontal KDL 2.0 unicode whitespace and nested `/* ... */` block comments."""
    chars = s.chars
    n = s.length
    i = start
    while i < n:
        m = _matchUnicodeSpace(chars, i)
        if m is not None:
            i = m.end()
            if i >= n:
                break
        if chars[i] == "/" and i + 1 < n and chars[i + 1] == "*":
            i = _skipBlockComment(s, i)
        else:
            break
    return i


def _skipBlockComment(s: Stream, start: int) -> int:
    """Skip a possibly nested `/* ... */` block comment starting at `start`, rejecting disallowed characters."""
    # Iterative depth scan with a single `/*|*/|disallowed` search regex avoids recursion and scans bodies in C.
    chars = s.chars
    if not chars.startswith("/*", start):
        return start
    depth = 1
    i = start + 2
    while depth > 0:
        m = _searchBlockCommentToken(chars, i)
        if m is None:
            raise ParseError(s, start, "Hit EOF while inside a multiline comment")
        tok = m.group()
        if tok == "/*":
            depth += 1
        elif tok == "*/":
            depth -= 1
        else:
            raise ParseError(
                s,
                m.start(),
                "Disallowed literal character in block comment.",
                maybeKdl1=tok == "\ufeff",
            )
        i = m.end()
    return i
