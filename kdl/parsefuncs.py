from __future__ import annotations

import re

from . import converters, kdl1check, parsing, t
from .chars import (
    BARE_IDENT_PATTERN,
    CONFUSABLE_KEYWORDS,
    DIGITS,
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
    Infinity,
    NaN,
    Node,
    Null,
    Octal,
    String,
    Value,
)

if t.TYPE_CHECKING:
    from .t import ParseResult
    from .types import LooseEntry

    # (lineStartPos, indent, text)
    MultilineLine: t.TypeAlias = tuple[int, str, str]

# 1-char lookahead sets used to skip unnecessary scanner calls (and dispatch bare idents in parseEntry).
_NODESPACE_START = frozenset(WS_CHARS + "/\\")
_LINESPACE_START = frozenset(WS_CHARS + NL_CHARS + "/\\")
_SPECIAL_ENTRY_START = frozenset('"{};\n\r)/(#0123456789+-')

# Precompiled scanner patterns and bound matchers
_matchUnicodeSpace = re.compile(rf"[{WS_RE}]+").match
_matchSingleLineComment = re.compile(rf"//[^{NL_RE}]*(?:\r\n|[{NL_RE}])?").match
_matchLinespaceChars = re.compile(rf"[{WS_RE}{NL_RE}]+").match
_searchBlockCommentToken = re.compile(r"/\*|\*/").search

_matchBareIdent = BARE_IDENT_PATTERN.match
_matchPlainQuoteChunk = re.compile(rf'[^"\\{NL_RE}]*').match
_matchRawQuoteChunk = re.compile(rf'[^"{NL_RE}]*').match

_matchDecimal = re.compile(r"([+-]?[0-9][0-9_]*(?:\.[0-9][0-9_]*)?)(?:[eE]([+-]?[0-9][0-9_]*))?").match
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


def parse(input: str, config: t.ParseConfig | None = None) -> Document:
    if config is None:
        config = parsing.defaults
    version = config.version
    if version == 1:
        from . import kdl1

        return kdl1.parse(input, config)
    if version not in (2, "auto"):
        msg = f"Invalid KDL version {version!r}; expected 1, 2, or 'auto'."
        raise ValueError(msg)

    allowFallback = version == "auto"
    s = Stream(input, config)
    chars = s.chars
    n = s.length
    i = 1 if chars.startswith("\ufeff") else 0
    if allowFallback and i + 2 <= n and chars[i] == "/" and chars[i + 1] == "-":
        markerVersion = kdl1check.detectVersionMarker(chars, i)
        if markerVersion == 1:
            from . import kdl1

            return kdl1.parse(input, config)
        if markerVersion == 2:
            allowFallback = False

    doc = Document()
    try:
        if i < n and chars[i] in _LINESPACE_START:
            i = skipLinespace(s, i)

        while i < n:
            node, nextI = parseBaseNode(s, i)
            if nextI < 0:
                break
            if node is not None:
                doc.nodes.append(node)
            termEnd = skipTerminatorAndLinespace(s, nextI)
            if termEnd < 0:
                got = chars[nextI] if nextI < n else ""
                raise ParseError(
                    s,
                    nextI,
                    f"Expected a node terminator (newline, ;, or EOF). Got '{got}'",
                    maybeKdl1=kdl1check.looksLikeAfterNode(chars, nextI, n),
                )
            i = termEnd

        if i < n:
            raise ParseError(
                s,
                i,
                "Unexpected non-node content",
                maybeKdl1=kdl1check.looksLikeNodeStart(chars, i, n),
            )
    except ParseError as err:
        if not allowFallback or not err.maybeKdl1:
            raise
        from . import kdl1

        try:
            return kdl1.parse(input, config)
        except ParseError:
            raise err from None
    return doc


def parseBaseNode(s: Stream, start: int, discard: bool = False) -> ParseResult[Node]:
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    if chars[start] == "/" and start + 1 < n and chars[start + 1] == "-":
        i = skipLinespace(s, start + 2)
        discard = True
    else:
        i = start

    tag: str | None = None
    if i < n and chars[i] == "(":
        tag, tagEnd = parseTag(s, i)
        if tagEnd < 0:
            return _FAIL
        i = skipNodespace(s, tagEnd) if (tagEnd < n and chars[tagEnd] in _NODESPACE_START) else tagEnd

    name, nameEnd = parseStringText(s, i)
    if name is None:
        return _FAIL
    i = nameEnd

    entries: list[LooseEntry] = []
    # Lazily allocated only if properties appear; maps key -> index in `entries` for O(1) duplicate replacement.
    propIndices: dict[str, int] | None = None

    # props and args
    while True:
        # Inline fast path for the common single-space separator to avoid calling skipNodespace().
        if i < n and chars[i] == " " and (i + 1 >= n or chars[i + 1] not in _NODESPACE_START):
            spaceEnd = i + 1
        elif i < n and chars[i] in _NODESPACE_START:
            spaceEnd = skipNodespace(s, i)
        else:
            spaceEnd = i
            break
        if spaceEnd >= n:
            break

        ch = chars[spaceEnd]
        # Fast exit when nodespace lands on a child block or common node terminator.
        if ch in "{};\n\r":
            break
        if ch == "/" and spaceEnd + 1 < n and chars[spaceEnd + 1] == "-":
            sdEnd = skipLinespace(s, spaceEnd + 2)
            entryDiscard = True
        elif spaceEnd == i:
            break
        else:
            sdEnd = spaceEnd
            entryDiscard = discard

        entry, nextI = parseEntry(s, sdEnd, discard=entryDiscard)
        if nextI < 0:
            break
        i = nextI
        if not entryDiscard:
            assert entry is not None
            propKey = entry[0]
            if propKey is None:
                entries.append(entry)
            elif propIndices is None:
                propIndices = {propKey: len(entries)}
                entries.append(entry)
            else:
                existingIdx = propIndices.get(propKey)
                if existingIdx is not None:
                    entries[existingIdx] = entry
                else:
                    propIndices[propKey] = len(entries)
                    entries.append(entry)

    # child blocks (optional real children plus any slashdashed child blocks)
    children: list[Node] = []
    hasRealChildren = False
    while spaceEnd < n:
        ch = chars[spaceEnd]
        if ch == "{":
            if hasRealChildren:
                break
            parsedChildren, i = parseNodeChildren(s, spaceEnd, discard=discard)
            assert parsedChildren is not None
            children = parsedChildren
            hasRealChildren = True
        elif ch == "/" and spaceEnd + 1 < n and chars[spaceEnd + 1] == "-":
            sdEnd = skipLinespace(s, spaceEnd + 2)
            sdChildren, childEnd = parseNodeChildren(s, sdEnd, discard=True)
            if sdChildren is None:
                break
            i = childEnd
        else:
            break
        spaceEnd = skipNodespace(s, i) if (i < n and chars[i] in _NODESPACE_START) else i

    i = spaceEnd
    if discard:
        return (None, i)

    node: Node = Node(name, tag, entries, children)
    nodeConverters = s.config.nodeConverters
    if nodeConverters:
        pf: ParseFragment | None = None
        for convKey, converter in nodeConverters.items():
            if node.matchesKey(convKey):
                if pf is None:
                    pf = ParseFragment(chars[start:nameEnd], s, start)
                converted = converter(node, pf)
                if converted == NotImplemented:
                    continue
                node = converted
                break

    return (node, i)


def parseNodeChildren(s: Stream, start: int, discard: bool = False) -> ParseResult[list[Node]]:
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "{":
        return _FAIL
    i = start + 1
    if i < n and chars[i] in _LINESPACE_START:
        i = skipLinespace(s, i)
    nodes: list[Node] = []

    while i < n and chars[i] != "}":
        node, nextI = parseBaseNode(s, i, discard=discard)
        if nextI < 0:
            break
        i = nextI
        if node is not None:
            nodes.append(node)
        termEnd = skipTerminatorAndLinespace(s, i)
        if termEnd < 0:
            break
        i = termEnd

    if i >= n:
        raise ParseError(s, start, "Hit EOF while searching for end of child list")
    if chars[i] != "}":
        raise ParseError(
            s,
            i,
            "Junk between end of child list and closing }",
            maybeKdl1=kdl1check.looksLikeAfterNode(chars, i, n) or kdl1check.looksLikeNodeStart(chars, i, n),
        )
    return (nodes, i + 1)


def parseTag(s: Stream, start: int) -> ParseResult[str]:
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "(":
        return _FAIL
    i = start + 1
    if i < n and chars[i] in _NODESPACE_START:
        i = skipNodespace(s, i)
    tag, i = parseStringText(s, i)
    if tag is None:
        return _FAIL
    if i < n and chars[i] in _NODESPACE_START:
        i = skipNodespace(s, i)
    if i >= n or chars[i] != ")":
        raise ParseError(
            s,
            i,
            "Junk between tag ident and closing paren.",
            maybeKdl1=kdl1check.looksLikeInTag(chars, i, n),
        )
    return (tag, i + 1)


def skipTerminatorAndLinespace(s: Stream, start: int) -> int:
    # Fuses node-terminator check and next-line linespace skipping: on a newline terminator,
    # `_matchLinespaceChars` eats the newline, blank lines, and next-line indentation in one regex call.
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
            return skipLinespace(s, i)
        return i
    if ch == ";":
        i = start + 1
        return skipLinespace(s, i) if (i < n and chars[i] in _LINESPACE_START) else i
    if ch == "/" and start + 1 < n and chars[start + 1] == "/":
        return skipLinespace(s, start)
    return -1


def parseEntry(s: Stream, start: int, discard: bool = False) -> ParseResult[LooseEntry]:
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    ch = chars[start]
    if ch not in _SPECIAL_ENTRY_START:
        # Fast path: bare identifier (either property key `key=val` or bare string arg)
        m = _matchBareIdent(chars, start)
        if m is None:
            return _FAIL
        strEnd = m.end()
        text = m.group()
        if text in CONFUSABLE_KEYWORDS:
            isPropKey = strEnd < n and chars[strEnd] == "="
            raise ParseError(
                s,
                start,
                f"Ident strings confusable with keywords aren't allowed; use a quoted string. Got '{text}'.",
                maybeKdl1=(not isPropKey) if text in kdl1check.KDL1_KEYWORDS else isPropKey,
            )
    elif ch == '"':
        textOrNone, strEnd = parseStringText(s, start)
        assert textOrNone is not None
        text = textOrNone
    elif ch in DIGITS or (ch in "+-" and start + 1 < n and chars[start + 1] in DIGITS):
        if discard or s.fastUntagged:
            val, valEnd = parseNumber(s, start, native=True)
            return (None, valEnd) if discard else ((None, val), valEnd)
        numVal, valEnd = parseNumber(s, start, native=False)
        val = _finalizeValue(s, t.cast("Value", numVal), None, start, valEnd)
        return ((None, val), valEnd)
    elif ch == "#":
        if start + 1 < n and chars[start + 1] not in ('"', "#"):
            rawFast = discard or s.fastUntagged
            kwVal, kwEnd = parseKeyword(s, start, native=rawFast)
            if kwEnd < 0:
                return _FAIL
            if discard:
                return (None, kwEnd)
            if rawFast:
                return ((None, kwVal), kwEnd)
            return ((None, _finalizeValue(s, t.cast("Value", kwVal), None, start, kwEnd)), kwEnd)
        textOrNone, strEnd = parseStringText(s, start)
        if textOrNone is None:
            return _FAIL
        text = textOrNone
    elif ch == "(":
        val, valEnd = parseValue(s, start, discard=discard)
        if valEnd < 0:
            return _FAIL
        return (None, valEnd) if discard else ((None, val), valEnd)
    elif ch in "+-":
        textOrNone, strEnd = parseStringText(s, start, inEntry=True)
        if textOrNone is None:
            return _FAIL
        text = textOrNone
    else:
        return _FAIL

    # Peek past optional nodespace for `=` to distinguish `key=val` properties from string args without backtracking.
    eqPos = -1
    if strEnd < n:
        nextCh = chars[strEnd]
        if nextCh == "=":
            eqPos = strEnd
        elif nextCh == " " and strEnd + 1 < n and chars[strEnd + 1] not in _NODESPACE_START:
            if chars[strEnd + 1] == "=":
                eqPos = strEnd + 1
        elif nextCh in _NODESPACE_START:
            afterKey = skipNodespace(s, strEnd)
            if afterKey < n and chars[afterKey] == "=":
                eqPos = afterKey

    if eqPos >= 0:
        valStart = eqPos + 1
        if valStart < n and chars[valStart] in _NODESPACE_START:
            valStart = skipNodespace(s, valStart)
        propVal, propEnd = parseValue(s, valStart, discard=discard)
        if propEnd < 0:
            raise ParseError(
                s,
                valStart,
                "Expected value after prop=.",
                maybeKdl1=kdl1check.isTagStartAt(chars, valStart, n),
            )
        return (None, propEnd) if discard else ((text, propVal), propEnd)

    if discard:
        return (None, strEnd)
    if s.fastUntagged:
        return ((None, text), strEnd)
    return ((None, _finalizeValue(s, String(text), None, start, strEnd)), strEnd)


def parseValue(s: Stream, start: int, discard: bool = False) -> ParseResult[t.Any]:
    chars = s.chars
    n = s.length
    if start >= n:
        return _FAIL

    tag: str | None = None
    i = start
    if chars[i] == "(":
        tag, tagEnd = parseTag(s, i)
        if tagEnd >= 0:
            i = skipNodespace(s, tagEnd) if (tagEnd < n and chars[tagEnd] in _NODESPACE_START) else tagEnd

    if i >= n:
        if tag is not None:
            raise ParseError(s, i, "Found a tag, but no value following it.")
        return _FAIL

    valueStart = i
    ch = chars[i]
    native = discard or (tag is None and s.fastUntagged)

    if ch in DIGITS or (ch in "+-" and i + 1 < n and chars[i + 1] in DIGITS):
        val, end = parseNumber(s, i, native=native)
        if discard:
            return (None, end)
        return (val, end) if native else (_finalizeValue(s, t.cast("Value", val), tag, valueStart, end), end)

    if ch == "#" and i + 1 < n and chars[i + 1] not in ('"', "#"):
        val, end = parseKeyword(s, i, native=native)
        if end < 0:
            if tag is not None:
                raise ParseError(s, i, "Found a tag, but no value following it.")
            return _FAIL
        if discard:
            return (None, end)
        return (val, end) if native else (_finalizeValue(s, t.cast("Value", val), tag, valueStart, end), end)

    text, end = parseStringText(s, i, inValue=True)
    if text is not None:
        if discard:
            return (None, end)
        return (text, end) if native else (_finalizeValue(s, String(text), tag, valueStart, end), end)

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
    val.tag = tag
    config = s.config
    valueConverters = config.valueConverters
    if valueConverters:
        pf: ParseFragment | None = None
        for key, converter in valueConverters.items():
            if val.matchesKey(key):
                if pf is None:
                    pf = ParseFragment(s.chars[valueStart:valueEnd], s, valueEnd)
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


def parseNumber(s: Stream, start: int, native: bool = False) -> ParseResult[t.Any]:
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
    elif ch in DIGITS:
        sign = 1
        i = start
    else:
        return _FAIL

    if chars[i] == "0" and i + 1 < n:
        prefix = chars[i + 1]
        if prefix == "x":
            m = _matchHexDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected hex digit after 0x, got junk.")
            raw = m.group()
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 16) * sign
            return (intVal if native else Hex(intVal), m.end())
        if prefix == "o":
            m = _matchOctDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected octal digit after 0o, got junk.")
            raw = m.group()
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 8) * sign
            return (intVal if native else Octal(intVal), m.end())
        if prefix == "b":
            m = _matchBinDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected binary digit after 0b, got junk.")
            raw = m.group()
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 2) * sign
            return (intVal if native else Binary(intVal), m.end())

    mDec = _matchDecimal(chars, start)
    assert mDec is not None
    end = mDec.end()
    # `_matchDecimal` stops before malformed suffixes like `1.` or `1e`; diagnose the exact error lazily.
    if end < n and chars[end] in ".eE":
        _raiseDecimalError(s, i)

    if native:
        raw = mDec.group()
        if "_" in raw:
            raw = raw.replace("_", "")
        fval = float(raw)
        # Match Decimal(int("-0"), 0).value (+0.0) while preserving negative zero for `-0.0`.
        if fval == 0.0 and "." not in raw:
            return (0.0, end)
        return (fval, end)

    mantStr, expStr = mDec.groups()
    if "_" in mantStr:
        mantStr = mantStr.replace("_", "")
    mantissa: int | float = float(mantStr) if "." in mantStr else int(mantStr, 10)

    if expStr is None:
        return (Decimal(mantissa, 0), end)

    if "_" in expStr:
        expStr = expStr.replace("_", "")
    exponent = int(expStr, 10)
    return (Decimal(mantissa, exponent), end)


def _raiseDecimalError(s: Stream, digitStart: int) -> t.NoReturn:
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


def parseKeyword(s: Stream, start: int, native: bool = False) -> ParseResult[t.Any]:
    chars = s.chars
    if start >= s.length or chars[start] != "#":
        return _FAIL
    m = _matchBareIdent(chars, start + 1)
    if m is None:
        return _FAIL
    ident = m.group()
    end = m.end()
    if native:
        val = NATIVE_KEYWORDS.get(ident, t.UNSET)
        if val is not t.UNSET:
            return (val, end)
    elif ident == "true":
        return (Bool(True), end)
    elif ident == "false":
        return (Bool(False), end)
    elif ident == "null":
        return (Null(), end)
    elif ident == "inf":
        return (Infinity(POS_INF), end)
    elif ident == "-inf":
        return (Infinity(NEG_INF), end)
    elif ident == "nan":
        return (NaN(NAN), end)

    isKdl1PropKey = end < s.length and chars[end] == "="
    if ident in CONFUSABLE_KEYWORDS:
        raise ParseError(s, start, f"KDL keywords must be written in lowercase, got #{ident}", maybeKdl1=isKdl1PropKey)
    raise ParseError(s, start, f"Unknown keyword #{ident}", maybeKdl1=isKdl1PropKey)


def parseStringText(s: Stream, start: int, *, inValue: bool = False, inEntry: bool = False) -> ParseResult[str]:
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
            # Fast path for short, plain, single-line strings; longer or escaped strings fall through.
            # isascii() is an O(1) header check that rules out non-ASCII KDL newlines (\x85, \u2028, \u2029).
            quotePos = chars.find('"', strStart, strStart + 256)
            if quotePos != -1:
                end = quotePos + 1
                if end >= n or chars[end] not in ('"', "#"):
                    sub = chars[strStart:quotePos]
                    if (
                        "\\" not in sub
                        and "\n" not in sub
                        and "\r" not in sub
                        and "\x0b" not in sub
                        and "\x0c" not in sub
                    ) and (sub.isascii() or ("\x85" not in sub and "\u2028" not in sub and "\u2029" not in sub)):
                        return (sub, end)
            return parseQuotedString(s, strStart, 0)
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
        return parseQuotedString(s, i, hashCount)
    if quoteCount == 2:
        # Empty single-line string `""` / `#""#`: back up 1 so parseQuotedString sees the closing `"`
        # and validates any trailing `#` or extra quotes.
        return parseQuotedString(s, i - 1, hashCount)
    if quoteCount == 3:
        return parseMultilineString(s, i, hashCount)
    raise ParseError(s, start, f"Encountered {quoteCount} quotes in a row.")


def _checkPlainQuoteEnd(s: Stream, start: int, chars: str, end: int, n: int) -> None:
    if end < n:
        nextCh = chars[end]
        if nextCh == "#":
            raise ParseError(s, start, "Saw # characters at the end of a non-raw string.")
        if nextCh == '"':
            raise ParseError(s, start, "Single-quote string was ended with multiple quote chars.")


def parseQuotedString(s: Stream, start: int, hashCount: int) -> ParseResult[str]:
    chars = s.chars
    n = s.length

    if hashCount == 0:
        m = _matchPlainQuoteChunk(chars, start)
        assert m is not None
        i = m.end()
        if i < n and chars[i] == '"':
            end = i + 1
            _checkPlainQuoteEnd(s, start, chars, end, n)
            return (chars[start:i], end)

        parts: list[str] = [chars[start:i]] if i > start else []
        while True:
            if i >= n:
                raise ParseError(s, start, "Hit EOF while looking for the end of the string")
            ch = chars[i]
            if ch == '"':
                end = i + 1
                _checkPlainQuoteEnd(s, start, chars, end, n)
                return ("".join(parts), end)
            if ch == "\\":
                esc, i = parseEscape(s, i)
                if esc:
                    parts.append(esc)
                m = _matchPlainQuoteChunk(chars, i)
                assert m is not None
                if m.end() > i:
                    parts.append(chars[i : m.end()])
                    i = m.end()
            else:
                raise ParseError(
                    s,
                    start,
                    "Saw an unescaped newline in a single-quote string.",
                    maybeKdl1=chars.find('"', i + 1) != -1,
                )

    # Raw single-line string (`#"..."#`)
    i = start
    while True:
        m = _matchRawQuoteChunk(chars, i)
        assert m is not None
        i = m.end()
        if i >= n:
            raise ParseError(s, start, "Hit EOF while looking for the end of the string")
        if chars[i] != '"':
            raise ParseError(s, start, "Saw an unescaped newline in a single-quote string.")
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


def parseEscape(s: Stream, start: int) -> ParseResult[str]:
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
        m = _matchLinespaceChars(chars, start + 1)
        assert m is not None
        return ("", m.end())
    raise ParseError(s, start, "Invalid character escape", maybeKdl1=ch == "/")


def parseMultilineString(s: Stream, start: int, hashCount: int) -> ParseResult[str]:
    chars = s.chars
    n = s.length
    i = skipNewline(chars, start, n)
    if i == start:
        raise ParseError(s, start, "Multiline strings must have a newline immediately after their opening quotes.")

    lines: list[MultilineLine] = []
    lineStart = i
    mSpace = _matchUnicodeSpace(chars, i)
    if mSpace is not None:
        lineIndent = chars[i : mSpace.end()]
        i = mSpace.end()
    else:
        lineIndent = ""
    lineParts: list[str] = []
    matchChunk = _matchPlainQuoteChunk if hashCount == 0 else _matchRawQuoteChunk

    while True:
        m = matchChunk(chars, i)
        assert m is not None
        if m.end() > i:
            lineParts.append(chars[i : m.end()])
            i = m.end()

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
                rawChars = processMultiline(lines, lineStart, lineIndent, "".join(lineParts), s)
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
                # which processMultiline() will reject as non-whitespace on the final line.
                lineParts.append('"' * (quoteCount - 3))
            rawChars = processMultiline(lines, lineStart, lineIndent, "".join(lineParts), s)
            return (rawChars, hashEnd)

        if ch == "\\":
            esc, i = parseEscape(s, i)
            if esc:
                lineParts.append(esc)
            continue

        # Must be a newline character
        nlEnd = skipNewline(chars, i, n)
        lines.append((lineStart, lineIndent, "".join(lineParts)))
        i = nlEnd
        lineStart = i
        mSpace = _matchUnicodeSpace(chars, i)
        if mSpace is not None:
            lineIndent = chars[i : mSpace.end()]
            i = mSpace.end()
        else:
            lineIndent = ""
        lineParts = []


def processMultiline(
    lines: list[MultilineLine],
    lastLinePos: int,
    lastIndent: str,
    lastText: str,
    s: Stream,
) -> str:
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


def skipNewline(chars: str, start: int, n: int) -> int:
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


def skipLinespace(s: Stream, start: int) -> int:
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
                continue
            if nextCh == "*":
                i = skipBlockComment(s, i)
                continue
            break
        if ch == "\\":
            nextI = skipEscline(s, i)
            if nextI == i:
                break
            i = nextI
        else:
            break
    return i


def skipNodespace(s: Stream, start: int) -> int:
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
        i = skipWhitespace(s, i)
        nextI = skipEscline(s, i)
        if nextI == i:
            return i
        i = nextI


def skipEscline(s: Stream, start: int) -> int:
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "\\":
        return start
    i = skipWhitespace(s, start + 1)
    if i >= n:
        return i
    nlEnd = skipNewline(chars, i, n)
    if nlEnd > i:
        return nlEnd
    if chars[i] == "/" and i + 1 < n and chars[i + 1] == "/":
        m = _matchSingleLineComment(chars, i)
        assert m is not None
        return m.end()
    return start


def skipWhitespace(s: Stream, start: int) -> int:
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
            i = skipBlockComment(s, i)
        else:
            break
    return i


def skipBlockComment(s: Stream, start: int) -> int:
    # Iterative depth scan with a single `/*|*/` search regex avoids recursion and scans bodies in C.
    chars = s.chars
    if not chars.startswith("/*", start):
        return start
    depth = 1
    i = start + 2
    while depth > 0:
        m = _searchBlockCommentToken(chars, i)
        if m is None:
            raise ParseError(s, start, "Hit EOF while inside a multiline comment")
        if m.group() == "/*":
            depth += 1
        else:
            depth -= 1
        i = m.end()
    return i
