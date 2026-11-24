from __future__ import annotations

import base64
import binascii
import datetime
import decimal
import ipaddress
import itertools
import operator
import re
import socket
import sys
import urllib.parse
import uuid

from . import converters, parsing, t
from .errors import ParseError, ParseFragment
from .stream import Stream
from .types import (
    Binary,
    Bool,
    Decimal,
    Document,
    Hex,
    Node,
    Null,
    Octal,
    RawString,
    String,
    Value,
    _scaleInt,
)

if t.TYPE_CHECKING:
    type _FastFrame = tuple[
        list[Node],  # 0: parentNodes
        t.Callable[[Node], None],  # 1: parentAppend
        str,  # 2: name
        str | None,  # 3: tag
        list[t.Any],  # 4: args
        dict[str, t.Any],  # 5: props
        bool,  # 6: nodeDiscard
        bool,  # 7: childDiscard
        bool,  # 8: parentDiscard
        int,  # 9: blockPos (exact offset if blockEnd < 0, else chunk pos)
        int,  # 10: blockEnd (chunk endPos if >= 0, else -1)
        int,  # 11: blockTokIdx (token index within chunk if blockEnd >= 0)
    ]

    type _GenFrame = tuple[
        list[Node],  # parentNodes
        int,  # nodeStart
        int,  # nameEnd
        str,  # name
        str | None,  # tag
        list[t.Any],  # args
        dict[str, t.Any],  # props
        bool,  # nodeDiscard
        bool,  # childDiscard
        bool,  # parentDiscard
        int,  # blockStart
    ]

# KDL 1.0 character classes (differ from KDL 2.0: BOM is whitespace, \x0b is not newline/WS,
# `<` and `>` are disallowed in bare idents, `#` and leading `.0` ARE allowed in bare idents).
_WS_CHARS = "\t \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000\ufeff"
_NL_CHARS = "\n\r\x85\x0c\u2028\u2029"
_WS_RE = r"\t \xa0\u1680\u2000-\u200a\u202f\u205f\u3000\ufeff"
_NL_RE = r"\n\r\x85\x0c\u2028\u2029"
_ID_CH = r'[^\x00-\x20\\/(){}<>;\[\]=,"\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'

_BARE_CORE = rf'(?![+-]?[0-9]|r#*"){_ID_CH}+'
_BARE_PROP_KEY = rf'(?![+-]?[0-9]|r#*"|(?:true|false|null)=){_ID_CH}+'
_DEC = r"[+-]?[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?"
_ID_EQ = r"[A-Za-z_][A-Za-z0-9_-]*"
_END = r"(?=[ \n{;]|$)"
_NUM_OR_RADIX = (
    r"[+-]?(?:0(?:x[0-9a-fA-F][0-9a-fA-F_]*|o[0-7][0-7_]*|b[01][01_]*)"
    + r"|[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?(?![.eExob]))"
)
_QPLAIN = r'"[^"\\]*"'
_QSTR = r'"[^"\\]*(?:\\(?:["\\/bfnrt]|u\{[0-9a-fA-F]{1,6}\})[^"\\]*)*"'
_RAW = r'r(?:"[^"]*"|#"[^"]*(?:"(?!#)[^"]*)*"#|##"[^"]*(?:"(?!##)[^"]*)*"##)(?![#=])'
_SLASH = r"/(?:-|\*[^*]*(?:\*(?!/)[^*]*)*\*/(?=[ \n{]))"

_WS_SET = frozenset(_WS_CHARS)
_NL_SET = frozenset(_NL_CHARS)
_DIGITS = frozenset("0123456789")
_KEYWORDS = frozenset(("true", "false", "null"))
_KW_NATIVE: dict[str, bool | None] = {"true": True, "false": False, "null": None}
_ATOMS: dict[str, t.Any] = {
    "true": True,
    "false": False,
    "null": None,
    **{str(n): n for n in range(-16, 256)},
}
_NATIVE_TAGS = converters.NATIVE_TAGS
_UNSET = t.UNSET
_lengthHint = operator.length_hint

_ISIZE_LIMIT = sys.maxsize + 1

_INT_TAG_BOUNDS: dict[str, tuple[int, int]] = {
    "i8": (-(2**7), 2**7),
    "i16": (-(2**15), 2**15),
    "i32": (-(2**31), 2**31),
    "i64": (-(2**63), 2**63),
    "i128": (-(2**127), 2**127),
    "u8": (0, 2**8),
    "u16": (0, 2**16),
    "u32": (0, 2**32),
    "u64": (0, 2**64),
    "u128": (0, 2**128),
    "isize": (-_ISIZE_LIMIT, _ISIZE_LIMIT),
    "usize": (0, 2 * _ISIZE_LIMIT),
}

_SIMPLE_ESCAPES: dict[str, str] = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    "/": "/",
    '"': '"',
    "b": "\b",
    "f": "\f",
}

# 7-group chunked tokenizer for C-speed scanning without Match allocations:
# (k, pstr, pv, astr, arg, nh, fb)
# - Captures plain quoted strings unquoted (`pstr`/`astr`) with guaranteed zero escapes.
# - Fuses `\n[ \t]*NAME "plain"` and `\n[ \t]*NAME {` into a single `nh` token.
# - Handles EOF/chunk-end `//` comment lines cleanly in the `\n` branch.
# - Trailing `(?s:.+)` in `fb` stops `findall` on any complex/error byte at exact offset `endPos - len(fb)`.
_PROP_VAL_RE = rf'(?:"([^"\\]*)"{_END}|({_NUM_OR_RADIX}|(?:true|false|null){_END}|{_QSTR}))'
_TOK_RE = re.compile(
    r" (?:"
    + rf"({_ID_EQ})={_PROP_VAL_RE}"
    + rf'|"([^"\\]+)"{_END}'
    + rf"|(\{{|{_NUM_OR_RADIX}|\({_ID_EQ}\)(?:{_QSTR}|{_NUM_OR_RADIX})"
    + rf"|{_QSTR}(?!=)|{_RAW}|{_SLASH}|\}}|\\[ \t]*\n[ \t]*(?= )|{_ID_EQ}{_END})"
    + r")"
    + rf"|\n(?:[ \t]*(?://[^{_NL_RE}]*)?\n)*[ \t]*(?:"
    + rf"(\}}|{_ID_EQ}(?: (?:{_QPLAIN}{_END}|\{{)|{_END})|/- {_ID_EQ}{_END}|\({_ID_EQ}\){_ID_EQ}{_END}|)"
    + rf"|//[^{_NL_RE}]*(?=$))"
    + r"|(\{|;|(?s:.+))",
)
_tokFindall = _TOK_RE.findall
_tokFinditer = _TOK_RE.finditer

# 6-group single-line tokenizer for the adaptive line-template cache tier:
# (k, pstr, pv, astr, arg, fb)
_LINE_TOK_FINDALL = re.compile(
    r" (?:"
    + rf"({_ID_EQ})={_PROP_VAL_RE}"
    + rf'|"([^"\\]+)"{_END}'
    + rf"|(\{{|{_NUM_OR_RADIX}|\({_ID_EQ}\)(?:{_QSTR}|{_NUM_OR_RADIX})"
    + rf"|{_QSTR}(?!=)|{_RAW}|{_SLASH}|\}}|\\[ \t]*\n[ \t]*(?= )|{_ID_EQ}{_END})"
    + r")"
    + rf"|[ \t]+(?:(?://[^{_NL_RE}]*)?$)|//[^{_NL_RE}]*$"
    + r"|(\{|;|(?s:.+))",
).findall
_LINE_HEAD_MATCH = re.compile(
    rf"(?:(/-)[ \t]+)?(?:\(({_ID_EQ})\))?({_ID_EQ})(?=[ \n{{;]|$)",
).match

_escSub = re.compile(r'\\(["\\/bfnrt]|u\{([0-9a-fA-F]{1,6})\})').sub

_dateCls = datetime.date
_dateFromIso = datetime.date.fromisoformat
_timeFromIso = datetime.time.fromisoformat
_dtFromIso = datetime.datetime.fromisoformat
_ipv4Cls = ipaddress.IPv4Address
_ipv6Cls = ipaddress.IPv6Address
_inetPton = socket.inet_pton
_AF_INET = socket.AF_INET
_AF_INET6 = socket.AF_INET6
_urlparse = urllib.parse.urlparse
_parseResultCls = urllib.parse.ParseResult
_uuidCls = uuid.UUID
_reCompile = re.compile
_a85decode = base64.a85decode
_b64decode = base64.b64decode
_decCls = decimal.Decimal

_simpleUrlFullmatch = re.compile(
    r"([a-z][a-z0-9+.-]*)://([A-Za-z0-9._-]+(?::[0-9]+)?)(/[A-Za-z0-9._~:/!$&'()*+,=%-]*)?",
).fullmatch

# Validators for quote-free ASCII and line-cached fast paths
_fastIdFull = re.compile(_ID_EQ).fullmatch
_hexFull = re.compile(r"[+-]?0(?:x[0-9a-fA-F][0-9a-fA-F_]*|o[0-7][0-7_]*|b[01][01_]*)").fullmatch
_decFull = re.compile(_DEC).fullmatch

# Exact regex matchers for fallback and custom-converter path
_matchNodeHead = re.compile(
    rf"(?:[{_WS_RE}{_NL_RE}]+|//[^{_NL_RE}]*)*(?:"
    + rf"({_BARE_CORE})"  # 1: bare node name
    + r'|"([^"\\]*)"'  # 2: plain quoted node name
    + r"|(\})"  # 3: closing brace
    + r"|(/-)"  # 4: slashdash
    + r"|(\()"  # 5: tag start
    + r"|(/\*)"  # 6: block comment
    + r'|(")'  # 7: escaped/unclosed quoted node name
    + r'|(r#*")'  # 8: raw string node name
    + r")?",
).match

_matchEntry = re.compile(
    r"(?:"
    + rf'({_BARE_PROP_KEY})(?:=(?:"([^"\\]*)"|({_DEC})(?![.eExob])|(true|false|null)(?!{_ID_CH})|()))?'
    + r'|"([^"\\]*)"(?!=)'
    + rf"|({_DEC})(?![.eExob])"
    + r")",
).match

_matchBareIdent = re.compile(_BARE_CORE).match
_matchBareTag = re.compile(rf"\(({_BARE_CORE})\)").match
_matchWs = re.compile(rf"[{_WS_RE}]+").match
_matchSingleLineComment = re.compile(rf"//[^{_NL_RE}]*(?:\r\n|[{_NL_RE}])?").match
_searchBlockCommentToken = re.compile(r"/\*|\*/").search
_matchHexEscape = re.compile(r"[0-9a-fA-F]*").match
_matchDecDigits = re.compile(r"[0-9][0-9_]*").match
_matchDecimal = re.compile(r"([+-]?[0-9][0-9_]*(?:\.[0-9][0-9_]*)?)(?:[eE]([+-]?[0-9][0-9_]*))?").match
_matchHexDigits = re.compile(r"[0-9a-fA-F][0-9a-fA-F_]*").match
_matchOctDigits = re.compile(r"[0-7][0-7_]*").match
_matchBinDigits = re.compile(r"[01][01_]*").match
_matchKeyword = re.compile(rf"(true|false|null)(?!{_ID_CH})").match


def _unescapeValidated(inner: str) -> str:
    """Unescape a string body already validated by `_QSTR`; raises ValueError on codepoint > 0x10FFFF."""
    if "\\\\" not in inner:
        if '\\"' in inner:
            inner = inner.replace('\\"', '"')
        if "\\n" in inner:
            inner = inner.replace("\\n", "\n")
        if "\\t" in inner:
            inner = inner.replace("\\t", "\t")
        if "\\" not in inner:
            return inner
        if "\\u" in inner:
            out: list[str] = []
            append = out.append
            find = inner.find
            pos = 0
            while True:
                u = find("\\u{", pos)
                if u == -1:
                    append(inner[pos:])
                    break
                if u > pos:
                    append(inner[pos:u])
                rb = find("}", u + 3)
                v = int(inner[u + 3 : rb], 16)
                if v > 0x10FFFF:
                    raise ValueError
                append(chr(v))
                pos = rb + 1
            inner = "".join(out)
            if "\\" not in inner:
                return inner
        if "\\r" in inner:
            inner = inner.replace("\\r", "\r")
        if "\\/" in inner:
            inner = inner.replace("\\/", "/")
        if "\\b" in inner:
            inner = inner.replace("\\b", "\b")
        if "\\f" in inner:
            inner = inner.replace("\\f", "\f")
        return inner
    return _escSub(_unescCb, inner)


def _unescCb(m: re.Match[str]) -> str:
    r"""Regex substitution callback decoding one simple or `\u{...}` KDL 1.0 escape match."""
    h = m.group(2)
    if h is not None:
        v = int(h, 16)
        if v > 0x10FFFF:
            raise ValueError
        return chr(v)
    return _SIMPLE_ESCAPES[m.group(1)]


def _skipBlockComment(s: Stream, start: int) -> int:
    """Skip a possibly nested `/* ... */` KDL 1.0 block comment starting at `start`."""
    chars = s.chars
    search = _searchBlockCommentToken
    starts: list[int] = [start]
    i = start + 2
    while starts:
        m = search(chars, i)
        if m is None:
            raise ParseError(s, starts[-1], "Hit EOF while inside a multiline comment")
        if m.group() == "/*":
            starts.append(m.start())
        else:
            starts.pop()
        i = m.end()
    return i


def _skipWhitespace(s: Stream, start: int) -> int:
    """Skip KDL 1.0 horizontal whitespace (including BOM) and nested `/* ... */` block comments."""
    chars = s.chars
    n = s.length
    i = start
    matchWs = _matchWs
    while i < n:
        m = matchWs(chars, i)
        if m is not None:
            i = m.end()
            if i >= n:
                break
        if chars[i] == "/" and i + 1 < n and chars[i + 1] == "*":
            i = _skipBlockComment(s, i)
        else:
            break
    return i


def _skipNodespace(s: Stream, start: int) -> int:
    r"""Skip KDL 1.0 intra-node whitespace, block comments, and `\` line continuations."""
    chars = s.chars
    n = s.length
    i = start
    nlSet = _NL_SET
    while i < n:
        i = _skipWhitespace(s, i)
        if i >= n or chars[i] != "\\":
            return i
        j = _skipWhitespace(s, i + 1)
        if j < n:
            ch = chars[j]
            if ch == "\n":
                i = j + 1
                continue
            if ch == "\r":
                i = j + 2 if (j + 1 < n and chars[j + 1] == "\n") else j + 1
                continue
            if ch in nlSet:
                i = j + 1
                continue
            if ch == "/" and j + 1 < n and chars[j + 1] == "/":
                m = _matchSingleLineComment(chars, j)
                assert m is not None
                i = m.end()
                continue
        return i
    return i


def _skipTerminator(s: Stream, i: int) -> int:
    """Consume a KDL 1.0 node terminator (newline, `;`, `//` comment, or EOF), or raise `ParseError`."""
    chars = s.chars
    n = s.length
    if i >= n:
        return i
    ch = chars[i]
    if ch in ("\n", ";"):
        return i + 1
    if ch == "\r":
        return i + 2 if (i + 1 < n and chars[i + 1] == "\n") else i + 1
    if ch in _NL_SET:
        return i + 1
    if ch == "/" and i + 1 < n and chars[i + 1] == "/":
        m = _matchSingleLineComment(chars, i)
        assert m is not None
        return m.end()
    raise ParseError(s, i, "Junk after node, before terminator.")


def _parseEscapedString(s: Stream, start: int) -> tuple[str, int]:
    """Scan a double-quoted KDL1 string using C-level `str.find` (zero regex Match allocations)."""
    chars = s.chars
    n = s.length
    find = chars.find
    i = start + 1
    q = find('"', i)
    b = find("\\", i)
    if b == -1 or (q != -1 and q < b):
        if q == -1:
            raise ParseError(s, start, "Hit EOF while looking for the end of the string")
        return (chars[i:q], q + 1)

    simpleEsc = _SIMPLE_ESCAPES
    parts: list[str] = []
    append = parts.append
    while True:
        if b > i:
            append(chars[i:b])
        if b + 1 >= n:
            raise ParseError(s, b, "Invalid character escape")
        escCh = chars[b + 1]
        mapped = simpleEsc.get(escCh)
        if mapped is not None:
            append(mapped)
            i = b + 2
        elif escCh == "u":
            if b + 2 >= n or chars[b + 2] != "{":
                raise ParseError(s, b, "Unicode escapes must surround their codepoint in {}")
            hexStart = b + 3
            mh = _matchHexEscape(chars, hexStart)
            assert mh is not None
            j = mh.end()
            hexCount = j - hexStart
            if j >= n or chars[j] != "}":
                raise ParseError(s, hexStart, "Expected } to finish a unicode escape")
            if hexCount < 1:
                raise ParseError(s, hexStart, "Unicode escape doesn't contain a codepoint")
            if hexCount > 6:
                raise ParseError(s, hexStart, "Unicode escapes can contain at most six digits")
            hexValue = int(chars[hexStart:j], 16)
            if hexValue > 0x10FFFF:
                raise ParseError(s, hexStart, "Maximum codepoint in a unicode escape is 0x10ffff")
            append(chr(hexValue))
            i = j + 1
        else:
            raise ParseError(s, b, "Invalid character escape")

        if q != -1 and q < i:
            q = find('"', i)
        b = find("\\", i)
        if b == -1 or (q != -1 and q < b):
            if q == -1:
                raise ParseError(s, start, "Hit EOF while looking for the end of the string")
            if q > i:
                append(chars[i:q])
            return ("".join(parts), q + 1)


def _parseRawString(s: Stream, start: int, hashCount: int) -> tuple[str, int]:
    """Parse a KDL 1.0 raw string (`r"..."` or `r#"..."#`) starting at `r`, validating closing hash count."""
    chars = s.chars
    n = s.length
    strStart = start + 2 + hashCount
    delim = '"' if hashCount == 0 else '"' + ("#" * hashCount)
    pos = chars.find(delim, strStart)
    if pos == -1:
        raise ParseError(s, start, "Hit EOF while looking for the end of the raw string.")
    end = pos + 1 + hashCount
    if end < n and chars[end] == "#":
        j = end + 1
        while j < n and chars[j] == "#":
            j += 1
        count = j - (pos + 1)
        raise ParseError(s, pos + 1, f"Expected {hashCount} hashes at end of raw string; got {count}.")
    return (chars[strStart:pos], end)


def _parseIdent(s: Stream, start: int) -> tuple[str | None, int]:
    """Parse a KDL 1.0 identifier (quoted, raw, or bare non-keyword) starting at `start`."""
    chars = s.chars
    n = s.length
    if start >= n:
        return (None, -1)
    ch = chars[start]
    if ch == '"':
        return _parseEscapedString(s, start)
    if ch == "r" and start + 1 < n and chars[start + 1] in ('"', "#"):
        j = start + 1
        while j < n and chars[j] == "#":
            j += 1
        if j < n and chars[j] == '"':
            return _parseRawString(s, start, j - start - 1)
    m = _matchBareIdent(chars, start)
    if m is None:
        return (None, -1)
    ident = m.group()
    if ident in _KEYWORDS:
        return (None, -1)
    return (ident, m.end())


def _parseTag(s: Stream, start: int) -> tuple[str | None, int]:
    """Parse a KDL 1.0 `(type)` annotation starting at `start` (no whitespace allowed inside parens)."""
    chars = s.chars
    n = s.length
    if start >= n or chars[start] != "(":
        return (None, -1)
    m = _matchBareTag(chars, start)
    if m is not None:
        tag = m.group(1)
        if tag not in _KEYWORDS:
            return (tag, m.end())
        return (None, -1)
    tagOrNone, end = _parseIdent(s, start + 1)
    if tagOrNone is None:
        return (None, -1)
    if end >= n or chars[end] != ")":
        raise ParseError(s, end, "Junk between tag ident and closing paren.")
    return (tagOrNone, end + 1)


def _decimalFromStr(raw: str) -> Decimal:
    """Convert a validated KDL 1.0 decimal literal into a `Decimal` preserving its exponent."""
    if "_" in raw:
        raw = raw.replace("_", "")
    ePos = raw.find("e")
    if ePos == -1:
        ePos = raw.find("E")
    try:
        if ePos == -1:
            mantissa: int | float = float(raw) if "." in raw else int(raw, 10)
            return Decimal(mantissa, 0)
        mantStr = raw[:ePos]
        expVal = int(raw[ePos + 1 :], 10)
        mantissa = float(mantStr) if "." in mantStr else int(mantStr, 10)
        return Decimal(mantissa, expVal)
    except ValueError:
        return Decimal(_floatFromDecStr(raw), 0)


def _floatFromDecStr(raw: str) -> float:
    """Convert a validated decimal string to `float`, normalizing integer `-0e-k` to `+0.0`."""
    if "_" in raw:
        raw = raw.replace("_", "")
    fval = float(raw)
    # Match Decimal(int("-0"), -k).value (+0.0) while preserving `-0.0` for explicit decimals and underflows.
    if fval == 0.0 and raw[0] == "-" and "." not in raw:
        ePos = raw.find("e")
        if ePos == -1:
            ePos = raw.find("E")
        if ePos == -1 or not raw[:ePos].strip("+-0"):
            return 0.0
    return fval


def _numFromDecStr(raw: str) -> float | int:
    """Convert a validated KDL 1.0 decimal literal into a native Python `int` or `float`."""
    if "_" in raw:
        raw = raw.replace("_", "")
    if "." not in raw:
        ePos = raw.find("e")
        if ePos == -1:
            ePos = raw.find("E")
        if ePos == -1:
            try:
                return int(raw, 10)
            except ValueError:
                pass
        else:
            try:
                expInt = int(raw[ePos + 1 :], 10)
                if expInt >= 0:
                    mInt = int(raw[:ePos], 10)
                    return mInt if expInt == 0 else _scaleInt(mInt, expInt)
            except ValueError:
                pass
    return _floatFromDecStr(raw)


def _decodeNumOther(pv: str) -> float | int:
    """Fast native decoder for non-simple numeric tokens (`0...`, signed, radix, and scientific notation)."""
    vc = pv[0]
    if vc == "0":
        if len(pv) == 1:
            return 0
        c1 = pv[1]
        if c1 == ".":
            try:
                return float(pv)
            except ValueError:
                return float(pv.replace("_", ""))
        if c1 in ("x", "o", "b"):
            try:
                return int(pv, 0)
            except ValueError:
                return int(pv.replace("_", ""), 0)
        return _numFromDecStr(pv)
    if vc in ("+", "-"):
        if len(pv) >= 3 and pv[2] in ("x", "o", "b"):
            try:
                return int(pv, 0)
            except ValueError:
                return int(pv.replace("_", ""), 0)
        if "." in pv:
            try:
                return float(pv)
            except ValueError:
                return float(pv.replace("_", ""))
        if "e" not in pv and "E" not in pv:
            try:
                return int(pv, 10)
            except ValueError:
                return _numFromDecStr(pv)
        return _numFromDecStr(pv)
    ePos = pv.find("e")
    if ePos == -1:
        ePos = pv.find("E")
    if ePos != -1 and len(pv) < 100:
        try:
            ev = int(pv[ePos + 1 :], 10)
            if 0 <= ev < 300:
                return t.cast("int", int(pv[:ePos], 10) * (10**ev))
        except ValueError:
            pass
    return _numFromDecStr(pv)


def _decodeRawNum(pv: str) -> Value:
    """Fast raw-mode decoder converting a validated numeric token into `Decimal`, `Hex`, `Octal`, or `Binary`."""
    vc = pv[0]
    if "1" <= vc <= "9":
        if "." not in pv and "e" not in pv and "E" not in pv:
            try:
                return Decimal(int(pv, 10), 0)
            except ValueError:
                return _decimalFromStr(pv)
        if "e" not in pv and "E" not in pv:
            try:
                return Decimal(float(pv), 0)
            except ValueError:
                return Decimal(float(pv.replace("_", "")), 0)
        return _decimalFromStr(pv)
    if vc == "0":
        if len(pv) == 1:
            return Decimal(0, 0)
        rc = pv[1]
        if rc in ("x", "o", "b"):
            try:
                iv = int(pv, 0)
            except ValueError:
                iv = int(pv.replace("_", ""), 0)
            return Hex(iv) if rc == "x" else (Octal(iv) if rc == "o" else Binary(iv))
        if rc == "." and "e" not in pv and "E" not in pv:
            try:
                return Decimal(float(pv), 0)
            except ValueError:
                return Decimal(float(pv.replace("_", "")), 0)
        return _decimalFromStr(pv)
    if len(pv) >= 3 and pv[2] in ("x", "o", "b"):
        try:
            iv = int(pv, 0)
        except ValueError:
            iv = int(pv.replace("_", ""), 0)
        rc = pv[2]
        return Hex(iv) if rc == "x" else (Octal(iv) if rc == "o" else Binary(iv))
    if "." not in pv and "e" not in pv and "E" not in pv:
        try:
            return Decimal(int(pv, 10), 0)
        except ValueError:
            return _decimalFromStr(pv)
    if "e" not in pv and "E" not in pv:
        try:
            return Decimal(float(pv), 0)
        except ValueError:
            return Decimal(float(pv.replace("_", "")), 0)
    return _decimalFromStr(pv)


def _parseNumber(s: Stream, start: int, native: bool, discard: bool) -> tuple[t.Any, int]:
    """Parse a KDL 1.0 decimal or radix (`0x`/`0o`/`0b`) literal at `start` with full error reporting."""
    chars = s.chars
    n = s.length
    ch = chars[start]
    if ch in ("+", "-"):
        sign = -1 if ch == "-" else 1
        i = start + 1
        if i >= n or chars[i] not in _DIGITS:
            return (None, -1)
    elif ch in _DIGITS:
        sign = 1
        i = start
    else:
        return (None, -1)

    if chars[i] == "0" and i + 1 < n:
        prefix = chars[i + 1]
        if prefix == "x":
            m = _matchHexDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected hex digit after 0x, got junk.")
            if discard:
                return (None, m.end())
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
            if discard:
                return (None, m.end())
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
            if discard:
                return (None, m.end())
            raw = m.group()
            try:
                intVal = int(raw, 2) * sign
            except ValueError:
                intVal = int(raw.replace("_", ""), 2) * sign
            return (intVal if native else Binary(intVal), m.end())

    mDec = _matchDecimal(chars, start)
    assert mDec is not None
    end = mDec.end()
    if end < n and chars[end] in (".", "e", "E"):
        mInt = _matchDecDigits(chars, i)
        assert mInt is not None
        p = mInt.end()
        if p < n and chars[p] == ".":
            mFrac = _matchDecDigits(chars, p + 1)
            if mFrac is None:
                raise ParseError(s, p + 1, "Expected digit after decimal point.")
            p = mFrac.end()
        if p < n and chars[p] in ("e", "E"):
            expDigits = p + 2 if (p + 1 < n and chars[p + 1] in ("+", "-")) else p + 1
            if _matchDecDigits(chars, expDigits) is None:
                raise ParseError(s, expDigits, "Expected number after exponent.")
    if discard:
        return (None, end)
    if native:
        return (_numFromDecStr(mDec.group()), end)
    mantStr = mDec.group(1)
    if "_" in mantStr:
        mantStr = mantStr.replace("_", "")
    expStr = mDec.group(2)
    try:
        mantissa: int | float = float(mantStr) if "." in mantStr else int(mantStr, 10)
        if expStr is None:
            return (Decimal(mantissa, 0), end)
        if "_" in expStr:
            expStr = expStr.replace("_", "")
        return (Decimal(mantissa, int(expStr, 10)), end)
    except ValueError:
        return (Decimal(_floatFromDecStr(mDec.group()), 0), end)


def _fastDate(text: str) -> t.Any:
    """Fast-path `(date)` converter returning a `datetime.date`, or `_UNSET` on invalid input."""
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            y = text[:4]
            mo = text[5:7]
            d = text[8:10]
            if y.isascii() and y.isdigit() and mo.isascii() and mo.isdigit() and d.isascii() and d.isdigit():
                return _dateCls(int(y), int(mo), int(d))
        return _dateFromIso(text)
    except ValueError:
        return _UNSET


def _fastIpv4(text: str) -> t.Any:
    """Fast-path `(ipv4)` converter using C `socket.inet_pton`, returning `_UNSET` on invalid input."""
    try:
        return _ipv4Cls(_inetPton(_AF_INET, text))
    except (OSError, ValueError):
        return _UNSET


def _fastUrl(text: str) -> t.Any:
    """Fast-path `(url)`/`(irl)` converter bypassing `urlparse` for simple `scheme://host/path` URLs."""
    m = _simpleUrlFullmatch(text)
    if m is not None:
        return _parseResultCls(m.group(1), m.group(2), m.group(3) or "", "", "", "")
    try:
        return _urlparse(text)
    except ValueError:
        return _UNSET


def _fastDateTime(text: str) -> t.Any:
    """Fast-path `(date-time)` converter returning a `datetime.datetime`, or `_UNSET` on failure."""
    try:
        return _dtFromIso(text)
    except ValueError:
        return _UNSET


def _fastTime(text: str) -> t.Any:
    """Fast-path `(time)` converter returning a `datetime.time`, or `_UNSET` on failure."""
    try:
        return _timeFromIso(text)
    except ValueError:
        return _UNSET


def _fastIpv6(text: str) -> t.Any:
    """Fast-path `(ipv6)` converter using `socket.inet_pton` (falling back to stdlib for `%scope` IDs)."""
    try:
        if "%" not in text:
            return _ipv6Cls(_inetPton(_AF_INET6, text))
        return _ipv6Cls(text)
    except (OSError, ValueError, ipaddress.AddressValueError):
        return _UNSET


def _fastUuid(text: str) -> t.Any:
    """Fast-path `(uuid)` converter returning a `uuid.UUID`, or `_UNSET` on failure."""
    try:
        return _uuidCls(text)
    except ValueError:
        return _UNSET


def _fastRegex(text: str) -> t.Any:
    """Fast-path `(regex)` converter returning a compiled `re.Pattern`, or `_UNSET` on failure."""
    try:
        return _reCompile(text)
    except re.error:
        return _UNSET


def _fastB64(text: str) -> t.Any:
    """Fast-path `(base64)` converter returning `bytes`, or `_UNSET` on invalid input."""
    try:
        return _b64decode(text.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError):
        return _UNSET


def _fastB85(text: str) -> t.Any:
    """Fast-path `(base85)` converter returning `bytes`, or `_UNSET` on invalid input."""
    try:
        return _a85decode(text.encode("utf-8"), adobe=text.startswith("<~") and text.endswith("~>"))
    except ValueError:
        return _UNSET


def _fastDecStr(text: str) -> t.Any:
    """Fast-path `(decimal)` string converter returning `decimal.Decimal`, or `_UNSET` on failure."""
    try:
        return _decCls(text)
    except decimal.InvalidOperation:
        return _UNSET


_TAG_STR_FAST: dict[str, t.Callable[[str], t.Any]] = {
    "date": _fastDate,
    "ipv4": _fastIpv4,
    "url": _fastUrl,
    "url-reference": _fastUrl,
    "irl": _fastUrl,
    "irl-reference": _fastUrl,
    "date-time": _fastDateTime,
    "time": _fastTime,
    "ipv6": _fastIpv6,
    "uuid": _fastUuid,
    "regex": _fastRegex,
    "base64": _fastB64,
    "base85": _fastB85,
    "decimal": _fastDecStr,
}
_tagStrGet = _TAG_STR_FAST.get


def _convertTaggedStringFast(tag: str, text: str) -> t.Any:
    """Convert a tagged string directly to its native type (or `String(text, tag)` if unknown; `_UNSET` on error)."""
    fn = _tagStrGet(tag)
    if fn is not None:
        return fn(text)
    return String(text, tag)


def _convertTaggedDecFast(tag: str, raw: str) -> t.Any:
    """Convert a tagged numeric literal directly to its native type (or tagged `Value`; `_UNSET` on error)."""
    bounds = _INT_TAG_BOUNDS.get(tag)
    if bounds is not None:
        if raw.isdigit():
            try:
                iv = int(raw, 10)
            except ValueError:
                return _UNSET
            return iv if bounds[0] <= iv < bounds[1] else _UNSET
        if len(raw) >= 2 and (
            raw[1] in ("x", "o", "b") or (raw[0] in ("+", "-") and len(raw) >= 3 and raw[2] in ("x", "o", "b"))
        ):
            try:
                iv = int(raw, 0)
            except ValueError:
                try:
                    iv = int(raw.replace("_", ""), 0)
                except ValueError:
                    return _UNSET
            return iv if bounds[0] <= iv < bounds[1] else _UNSET
        cleaned = raw.replace("_", "") if "_" in raw else raw
        if "." not in cleaned and "e" not in cleaned and "E" not in cleaned:
            try:
                iv = int(cleaned, 10)
            except ValueError:
                return _UNSET
            return iv if bounds[0] <= iv < bounds[1] else _UNSET
        try:
            dv = _decCls(cleaned)
        except decimal.InvalidOperation:
            return _UNSET
        if dv.is_finite() and dv == dv.to_integral_value() and bounds[0] <= dv < bounds[1]:
            return int(dv)
        return _UNSET
    if tag in ("f32", "f64"):
        if len(raw) >= 2 and (
            raw[1] in ("x", "o", "b") or (raw[0] in ("+", "-") and len(raw) >= 3 and raw[2] in ("x", "o", "b"))
        ):
            return _UNSET
        return _floatFromDecStr(raw)
    if tag in ("decimal64", "decimal128"):
        if len(raw) >= 2 and (
            raw[1] in ("x", "o", "b") or (raw[0] in ("+", "-") and len(raw) >= 3 and raw[2] in ("x", "o", "b"))
        ):
            return _UNSET
        try:
            return _decCls(raw.replace("_", "") if "_" in raw else raw)
        except decimal.InvalidOperation:
            return _UNSET
    v = _decodeRawNum(raw)
    v.tag = tag
    return v


def _finalizeValue(s: Stream, val: Value, tag: str | None, valStart: int, valEnd: int) -> t.Any:
    """Attach `tag`, run user `valueConverters`, and unwrap or apply built-in native tag conversions."""
    val.tag = tag
    config = s.config
    valueConverters = config.valueConverters
    if valueConverters:
        pf: ParseFragment | None = None
        for key, converter in valueConverters.items():
            if val.matchesKey(key):
                if pf is None:
                    pf = ParseFragment(s.chars[valStart:valEnd], s, valStart)
                converted = converter(val, pf)
                if converted == NotImplemented:
                    continue
                return converted

    if tag is None:
        if config.nativeUntaggedValues:
            return val.value
        return val

    if config.nativeTaggedValues and tag in _NATIVE_TAGS:
        return converters.toNative(val, s, valStart, valEnd)
    return val


def _parseValueFast(s: Stream, start: int, discard: bool, nativeTagged: bool) -> tuple[t.Any, int]:
    """Parse one KDL 1.0 value when `nativeUntaggedValues=True` and no custom value converters are active."""
    chars = s.chars
    n = s.length
    if start >= n:
        return (None, -1)

    tag: str | None = None
    i = start
    if chars[i] == "(":
        mt = _matchBareTag(chars, i)
        if mt is not None:
            cand = mt.group(1)
            if cand not in _KEYWORDS:
                tag = cand
                i = mt.end()
        else:
            tag, tagEnd = _parseTag(s, i)
            if tagEnd >= 0:
                i = tagEnd

    if i >= n:
        if tag is not None:
            raise ParseError(s, i, "Found a tag, but no value following it.")
        return (None, -1)

    valStart = i
    ch = chars[i]
    untagged = tag is None
    val: t.Any
    end: int

    if ch in _DIGITS or (ch in ("+", "-") and i + 1 < n and chars[i + 1] in _DIGITS):
        val, end = _parseNumber(s, i, untagged, discard)
        if discard or untagged:
            return (val, end)
        vObj = t.cast("Value", val)
        vObj.tag = tag
        if nativeTagged and tag in _NATIVE_TAGS:
            return (converters.toNative(vObj, s, valStart, end), end)
        return (vObj, end)

    if ch == '"':
        strVal, end = _parseEscapedString(s, i)
        if discard:
            return (None, end)
        if untagged:
            return (strVal, end)
        if nativeTagged and tag is not None:
            conv = _convertTaggedStringFast(tag, strVal)
            if conv is not _UNSET:
                return (conv, end)
        sObj = String(strVal, tag)
        if nativeTagged and tag in _NATIVE_TAGS:
            return (converters.toNative(sObj, s, valStart, end), end)
        return (sObj, end)

    if ch == "r" and i + 1 < n and chars[i + 1] in ('"', "#"):
        j = i + 1
        while j < n and chars[j] == "#":
            j += 1
        if j < n and chars[j] == '"':
            rawVal, end = _parseRawString(s, i, j - i - 1)
            if discard:
                return (None, end)
            if untagged:
                return (rawVal, end)
            rObj = RawString(rawVal, tag)
            if nativeTagged and tag in _NATIVE_TAGS:
                return (converters.toNative(rObj, s, valStart, end), end)
            return (rObj, end)

    if ch in ("t", "f", "n"):
        mk = _matchKeyword(chars, i)
        if mk is not None:
            end = mk.end()
            if discard:
                return (None, end)
            kw = mk.group()
            if untagged:
                return (_KW_NATIVE[kw], end)
            kObj: Value = Bool(True, tag) if kw == "true" else (Bool(False, tag) if kw == "false" else Null(tag))
            return (kObj, end)

    if ch == "'":
        raise ParseError(s, i, "KDL strings use double-quotes.")

    mb = _matchBareIdent(chars, i)
    if mb is not None and mb.group().lower() in _KEYWORDS:
        raise ParseError(s, i, "KDL keywords are lower-case.")

    if tag is not None:
        raise ParseError(s, i, "Found a tag, but no value following it.")
    return (None, -1)


def _parseValue(s: Stream, start: int, fastUntagged: bool, discard: bool) -> tuple[t.Any, int]:
    """Parse one KDL 1.0 value with full `_finalizeValue` support (raw mode or custom converters)."""
    chars = s.chars
    n = s.length
    if start >= n:
        return (None, -1)

    tag: str | None = None
    i = start
    if chars[i] == "(":
        tag, tagEnd = _parseTag(s, i)
        if tagEnd >= 0:
            i = tagEnd

    if i >= n:
        if tag is not None:
            raise ParseError(s, i, "Found a tag, but no value following it.")
        return (None, -1)

    valStart = i
    ch = chars[i]
    native = discard or (tag is None and fastUntagged)
    val: t.Any
    end: int

    if ch in _DIGITS or (ch in ("+", "-") and i + 1 < n and chars[i + 1] in _DIGITS):
        val, end = _parseNumber(s, i, native, discard)
        if discard:
            return (None, end)
        if native:
            return (val, end)
        return (_finalizeValue(s, t.cast("Value", val), tag, valStart, end), end)

    if ch == '"':
        strVal, end = _parseEscapedString(s, i)
        if discard:
            return (None, end)
        if native:
            return (strVal, end)
        return (_finalizeValue(s, String(strVal), tag, valStart, end), end)

    if ch == "r" and i + 1 < n and chars[i + 1] in ('"', "#"):
        j = i + 1
        while j < n and chars[j] == "#":
            j += 1
        if j < n and chars[j] == '"':
            rawVal, end = _parseRawString(s, i, j - i - 1)
            if discard:
                return (None, end)
            if native:
                return (rawVal, end)
            return (_finalizeValue(s, RawString(rawVal), tag, valStart, end), end)

    if ch in ("t", "f", "n"):
        mk = _matchKeyword(chars, i)
        if mk is not None:
            end = mk.end()
            if discard:
                return (None, end)
            kw = mk.group()
            if native:
                return (_KW_NATIVE[kw], end)
            kwObj: Value = Bool(True) if kw == "true" else (Bool(False) if kw == "false" else Null())
            return (_finalizeValue(s, kwObj, tag, valStart, end), end)

    if ch == "'":
        raise ParseError(s, i, "KDL strings use double-quotes.")

    mb = _matchBareIdent(chars, i)
    if mb is not None and mb.group().lower() in _KEYWORDS:
        raise ParseError(s, i, "KDL keywords are lower-case.")

    if tag is not None:
        raise ParseError(s, i, "Found a tag, but no value following it.")
    return (None, -1)


def _parseDiscardedEntity(s: Stream, start: int) -> int:
    """Validate and skip a slashdashed (`/-`) KDL 1.0 arg or property, returning its end offset or -1."""
    chars = s.chars
    n = s.length
    if start >= n:
        return -1
    m = _matchEntry(chars, start)
    if m is not None:
        li = m.lastindex
        if li in (2, 3, 4, 6, 7):
            return m.end()
        if li == 5:
            _, valEnd = _parseValueFast(s, m.end(), True, False)
            if valEnd < 0:
                raise ParseError(s, m.end(), "Expected value after prop=.")
            return valEnd
        tok = m.group(1)
        if tok in _KEYWORDS:
            return m.end()
        if tok.lower() in _KEYWORDS:
            raise ParseError(s, start, "KDL keywords are lower-case.")
        return -1

    ch = chars[start]
    if ch == '"':
        _, strEnd = _parseEscapedString(s, start)
        if strEnd < n and chars[strEnd] == "=":
            _, valEnd = _parseValueFast(s, strEnd + 1, True, False)
            if valEnd < 0:
                raise ParseError(s, strEnd + 1, "Expected value after prop=.")
            return valEnd
        return strEnd
    if ch == "r" and start + 1 < n and chars[start + 1] in ('"', "#"):
        j = start + 1
        while j < n and chars[j] == "#":
            j += 1
        if j < n and chars[j] == '"':
            _, strEnd = _parseRawString(s, start, j - start - 1)
            if strEnd < n and chars[strEnd] == "=":
                _, valEnd = _parseValueFast(s, strEnd + 1, True, False)
                if valEnd < 0:
                    raise ParseError(s, strEnd + 1, "Expected value after prop=.")
                return valEnd
            return strEnd
    _, valEnd = _parseValueFast(s, start, True, False)
    return valEnd


def _pickChunkEnd(chars: str, pos: int, n: int, chunkSize: int) -> int:
    """Choose a chunk boundary near `pos + chunkSize` on a newline or space (or `-1` if none exists)."""
    # Splitting on `\n`/` ` keeps chunks token-aligned while bounding scans so errors fail fast in O(1).
    target = pos + chunkSize
    if target >= n:
        return n
    nlPos = chars.find("\n", target, target + 512)
    if nlPos != -1:
        return nlPos
    spPos = chars.find(" ", target, target + 512)
    if spPos != -1:
        return spPos
    prevNl = chars.rfind("\n", pos + 1, target)
    if prevNl != -1:
        return prevNl
    prevSp = chars.rfind(" ", pos + 1, target)
    if prevSp != -1:
        return prevSp
    return -1


def _tokenOffset(chars: str, pos: int, endPos: int, tokIdx: int) -> int:
    """Re-scan the current chunk lazily with `finditer` to find the exact byte offset of token `tokIdx`."""
    m = next(itertools.islice(_tokFinditer(chars, pos, endPos), tokIdx, tokIdx + 1))
    return m.start()


def _unclosedBlockPos(chars: str, frame: _FastFrame) -> int:
    """Resolve the exact byte offset of the unclosed `{` stored in `frame` for `ParseError` reporting."""
    bPos = frame[9]
    bEnd = frame[10]
    if bEnd < 0:
        return bPos
    # For `' {'`, bare `'{'`, and fused `'\n...name {'`, the `{` is always the final char of the token match.
    m = next(itertools.islice(_tokFinditer(chars, bPos, bEnd), frame[11], frame[11] + 1))
    return m.end() - 1


def _hasRepeatingLines(lines: list[str]) -> bool:
    """Fast O(1) sample probe: True iff >= 20% of non-trivial lines in the sample repeat."""
    seen: set[str] = set()
    seenAdd = seen.add
    nonTrivial = 0
    for ln in lines[:48]:
        if len(ln) > 4:
            nonTrivial += 1
            seenAdd(ln)
    return nonTrivial >= 8 and len(seen) * 5 <= nonTrivial * 4


def _handleChunkFallback(
    s: Stream,
    i: int,
    *,
    nativeUntagged: bool,
    nativeTagged: bool,
    state: int,
    name: str,
    tag: str | None,
    args: list[t.Any],
    props: dict[str, t.Any],
    nodeDiscard: bool,
    curNodes: list[Node],
    curDiscard: bool,
    stack: list[_FastFrame],
) -> tuple[
    int,
    int,
    str,
    str | None,
    list[t.Any],
    dict[str, t.Any],
    bool,
    list[Node],
    bool,
]:
    """Execute one exact fallback step at byte offset `i` for `_parseChunkedNative`/`_parseChunkedRaw`."""
    chars = s.chars
    n = s.length
    keywords = _KEYWORDS
    wsSet = _WS_SET
    nlSet = _NL_SET

    if state == 0:
        mh = _matchNodeHead(chars, i)
        assert mh is not None
        li = mh.lastindex
        if li == 1:
            cand = mh.group(1)
            if cand in keywords:
                raise ParseError(
                    s,
                    mh.start(1),
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            return (mh.end(), 1, cand, None, [], {}, curDiscard, curNodes, curDiscard)
        if li == 3:
            if not stack:
                raise ParseError(s, mh.start(3), "Expected a node")
            (
                parentNodes,
                parentAppend,
                pName,
                pTag,
                pArgs,
                pProps,
                pNodeDiscard,
                pChildDiscard,
                pParentDiscard,
                _,
                _,
                _,
            ) = stack.pop()
            if not pNodeDiscard:
                parentAppend(Node(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes))
            return (mh.end(), 2, "", None, [], {}, False, parentNodes, pParentDiscard)
        if li is None:
            end = mh.end()
            if end >= n:
                if stack:
                    raise ParseError(
                        s,
                        _unclosedBlockPos(chars, stack[-1]),
                        "Hit EOF while searching for end of child list",
                    )
                return (-1, 0, "", None, [], {}, False, curNodes, curDiscard)
            raise ParseError(
                s,
                end,
                "Junk between end of child list and closing }" if stack else "Expected a node",
            )
        if li == 2:
            return (mh.end(), 1, mh.group(2), None, [], {}, curDiscard, curNodes, curDiscard)
        if li == 5:
            tagStart = mh.start(5)
            newTag, tagEnd = _parseTag(s, tagStart)
            if tagEnd < 0:
                raise ParseError(
                    s,
                    tagStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            nameOrNone, nameEnd = _parseIdent(s, tagEnd)
            if nameOrNone is None:
                raise ParseError(
                    s,
                    tagStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            return (nameEnd, 1, nameOrNone, newTag, [], {}, curDiscard, curNodes, curDiscard)
        if li == 4:
            sdStart = mh.start(4)
            j = _skipNodespace(s, mh.end())
            newTag = None
            if j < n and chars[j] == "(":
                newTag, tagEnd = _parseTag(s, j)
                if tagEnd >= 0:
                    j = tagEnd
            nameOrNone, nameEnd = _parseIdent(s, j)
            if nameOrNone is None:
                raise ParseError(
                    s,
                    sdStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            return (nameEnd, 1, nameOrNone, newTag, [], {}, True, curNodes, curDiscard)
        if li == 6:
            return (_skipBlockComment(s, mh.start(6)), 0, "", None, [], {}, False, curNodes, curDiscard)
        if li == 7:
            qName, qEnd = _parseEscapedString(s, mh.start(7))
            return (qEnd, 1, qName, None, [], {}, curDiscard, curNodes, curDiscard)
        rawStart = mh.start(8)
        rName, rEnd = _parseRawString(s, rawStart, mh.end() - rawStart - 2)
        return (rEnd, 1, rName, None, [], {}, curDiscard, curNodes, curDiscard)

    if state == 2:
        if i < n:
            chEnd = chars[i]
            if chEnd in wsSet or chEnd == "\\" or (chEnd == "/" and i + 1 < n and chars[i + 1] == "*"):
                i = _skipNodespace(s, i)
            i = _skipTerminator(s, i)
        return (i, 0, "", None, [], {}, False, curNodes, curDiscard)

    # state == 1
    ch = chars[i]
    hadSpace = False
    if ch in wsSet or ch == "\\" or (ch == "/" and i + 1 < n and chars[i + 1] == "*"):
        nextI = _skipNodespace(s, i)
        if nextI > i:
            i = nextI
            hadSpace = True
    if not hadSpace:
        if ch == "{":
            stack.append(
                (curNodes, curNodes.append, name, tag, args, props, nodeDiscard, nodeDiscard, curDiscard, i, -1, 0),
            )
            return (i + 1, 0, "", None, [], {}, False, [], nodeDiscard)
        if ch == "/" and i + 1 < n and chars[i + 1] == "-":
            j = _skipNodespace(s, i + 2)
            if j < n and chars[j] == "{":
                stack.append(
                    (curNodes, curNodes.append, name, tag, args, props, nodeDiscard, True, curDiscard, i, -1, 0),
                )
                return (j + 1, 0, "", None, [], {}, False, [], True)
            raise ParseError(s, i, "Junk after node, before terminator.")
        i = _skipTerminator(s, i)
        if not nodeDiscard:
            curNodes.append(Node(name, tag, args, props, []))
        return (i, 0, "", None, [], {}, False, curNodes, curDiscard)

    if i >= n:
        if not nodeDiscard:
            curNodes.append(Node(name, tag, args, props, []))
        return (n, 0, "", None, [], {}, False, curNodes, curDiscard)

    ch2 = chars[i]
    if ch2 == "{":
        stack.append(
            (curNodes, curNodes.append, name, tag, args, props, nodeDiscard, nodeDiscard, curDiscard, i, -1, 0),
        )
        return (i + 1, 0, "", None, [], {}, False, [], nodeDiscard)
    if ch2 in ("\n", ";") or ch2 in nlSet or (ch2 == "/" and i + 1 < n and chars[i + 1] == "/"):
        i = _skipTerminator(s, i)
        if not nodeDiscard:
            curNodes.append(Node(name, tag, args, props, []))
        return (i, 0, "", None, [], {}, False, curNodes, curDiscard)
    if ch2 == "/" and i + 1 < n and chars[i + 1] == "-":
        sdStart = i
        j = _skipNodespace(s, i + 2)
        if j < n and chars[j] == "{":
            stack.append(
                (curNodes, curNodes.append, name, tag, args, props, nodeDiscard, True, curDiscard, sdStart, -1, 0),
            )
            return (j + 1, 0, "", None, [], {}, False, [], True)
        entEnd = _parseDiscardedEntity(s, j)
        if entEnd < 0:
            raise ParseError(s, sdStart, "Junk after node, before terminator.")
        return (entEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)

    m = _matchEntry(chars, i)
    if m is not None:
        eli = m.lastindex
        if eli in (2, 3, 4):
            endPos = m.end()
            if not nodeDiscard:
                key = m.group(1)
                if eli == 3:
                    rawNum = m.group(3)
                    pval: t.Any = _numFromDecStr(rawNum) if nativeUntagged else _decimalFromStr(rawNum)
                elif eli == 2:
                    sVal = m.group(2)
                    pval = sVal if nativeUntagged else String(sVal)
                else:
                    kwStr = m.group(4)
                    pval = (
                        _KW_NATIVE[kwStr]
                        if nativeUntagged
                        else (Bool(True) if kwStr == "true" else (Bool(False) if kwStr == "false" else Null()))
                    )
                props[key] = pval
            return (endPos, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if eli == 5:
            key = m.group(1)
            valStart = m.end()
            val, valEnd = (
                _parseValueFast(s, valStart, nodeDiscard, nativeTagged)
                if nativeUntagged
                else _parseValue(s, valStart, False, nodeDiscard)
            )
            if valEnd < 0:
                raise ParseError(s, valStart, "Expected value after prop=.")
            if not nodeDiscard:
                props[key] = val
            return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if eli == 6:
            if not nodeDiscard:
                sVal = m.group(6)
                args.append(sVal if nativeUntagged else String(sVal))
            return (m.end(), 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if eli == 7:
            if not nodeDiscard:
                rawNum = m.group(7)
                args.append(_numFromDecStr(rawNum) if nativeUntagged else _decimalFromStr(rawNum))
            return (m.end(), 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        tok1 = m.group(1)
        kwVal = _KW_NATIVE.get(tok1, _UNSET)
        if kwVal is not _UNSET:
            if not nodeDiscard:
                val = (
                    kwVal
                    if nativeUntagged
                    else (Bool(True) if tok1 == "true" else (Bool(False) if tok1 == "false" else Null()))
                )
                args.append(val)
            return (m.end(), 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if tok1.lower() in keywords:
            raise ParseError(s, i, "KDL keywords are lower-case.")
        raise ParseError(s, i, "Junk after node, before terminator.")

    if ch2 == '"':
        strVal, strEnd = _parseEscapedString(s, i)
        if strEnd < n and chars[strEnd] == "=":
            val, valEnd = (
                _parseValueFast(s, strEnd + 1, nodeDiscard, nativeTagged)
                if nativeUntagged
                else _parseValue(s, strEnd + 1, False, nodeDiscard)
            )
            if valEnd < 0:
                raise ParseError(s, strEnd + 1, "Expected value after prop=.")
            if not nodeDiscard:
                props[strVal] = val
            return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if not nodeDiscard:
            args.append(strVal if nativeUntagged else String(strVal))
        return (strEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)

    if ch2 == "r" and i + 1 < n and chars[i + 1] in ('"', "#"):
        j = i + 1
        while j < n and chars[j] == "#":
            j += 1
        if j < n and chars[j] == '"':
            rawVal, strEnd = _parseRawString(s, i, j - i - 1)
            if strEnd < n and chars[strEnd] == "=":
                val, valEnd = (
                    _parseValueFast(s, strEnd + 1, nodeDiscard, nativeTagged)
                    if nativeUntagged
                    else _parseValue(s, strEnd + 1, False, nodeDiscard)
                )
                if valEnd < 0:
                    raise ParseError(s, strEnd + 1, "Expected value after prop=.")
                if not nodeDiscard:
                    props[rawVal] = val
                return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
            if not nodeDiscard:
                args.append(rawVal if nativeUntagged else RawString(rawVal))
            return (strEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)

    val, valEnd = (
        _parseValueFast(s, i, nodeDiscard, nativeTagged) if nativeUntagged else _parseValue(s, i, False, nodeDiscard)
    )
    if valEnd < 0:
        raise ParseError(s, i, "Junk after node, before terminator.")
    if not nodeDiscard:
        args.append(val)
    return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)


def _tryQuoteFreeChunkNative(
    chars: str,
    pos: int,
    endPos: int,
    *,
    curNodes: list[Node],
    stack: list[_FastFrame],
    validIds: set[str],
) -> tuple[int, list[Node]]:
    """
    Ultra-fast line-split parser for pure-ASCII chunks with no quotes/semicolons/comments/parens/escapes.
    Returns `(newPos, curNodes)` — stops at exact line byte offset on any line needing fallback.
    """
    chunk = chars[pos:endPos]
    lines = chunk.split("\n")
    noUnder = "_" not in chunk
    nodeCls = Node
    intFn = int
    floatFn = float
    atomsGet = _ATOMS.get
    keywords = _KEYWORDS
    fastId = _fastIdFull
    hexFull = _hexFull
    decFull = _decFull
    numDecFn = _numFromDecStr
    unset = _UNSET
    validAdd = validIds.add
    curAppend = curNodes.append
    stackPush = stack.append
    stackPop = stack.pop

    lineOffset = pos
    for rawLine in lines:
        lineLen = len(rawLine)
        parts = rawLine.split()
        if not parts:
            lineOffset += lineLen + 1
            continue
        p0 = parts[0]
        if p0 == "}":
            if len(parts) != 1 or not stack:
                return (lineOffset, curNodes)
            parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, _, _, _, _ = stackPop()
            if not pND:
                parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
            curNodes = parentNodes
            curAppend = parentAppend
            lineOffset += lineLen + 1
            continue
        if p0[0] == "/" and p0.startswith("//"):
            lineOffset += lineLen + 1
            continue
        if p0 not in validIds:
            if fastId(p0) is None or p0 in keywords:
                return (lineOffset, curNodes)
            validAdd(p0)

        nParts = len(parts)
        if nParts == 1:
            curAppend(nodeCls(p0, None, [], {}, []))
            lineOffset += lineLen + 1
            continue

        hasBrace = parts[-1] == "{"
        limit = nParts - 1 if hasBrace else nParts
        args: list[t.Any] = []
        argsAppend = args.append
        props: dict[str, t.Any] = {}
        ok = True
        idx = 1
        while idx < limit:
            tok = parts[idx]
            idx += 1
            av = atomsGet(tok, unset)
            if av is not unset:
                argsAppend(av)
                continue
            c0 = tok[0]
            if "1" <= c0 <= "9":
                if noUnder:
                    if "." not in tok and "e" not in tok and "E" not in tok:
                        try:
                            argsAppend(intFn(tok, 10))
                            continue
                        except ValueError:
                            pass
                    elif "." in tok:
                        dot = tok.find(".")
                        if dot + 1 < len(tok) and "0" <= tok[dot + 1] <= "9":
                            try:
                                argsAppend(floatFn(tok))
                                continue
                            except ValueError:
                                pass
                    else:
                        ePos = tok.find("e")
                        if ePos == -1:
                            ePos = tok.find("E")
                        if ePos != -1 and ePos + 1 < len(tok) and len(tok) < 100:
                            cNext = tok[ePos + 1]
                            if "0" <= cNext <= "9" or (
                                cNext in ("+", "-") and ePos + 2 < len(tok) and "0" <= tok[ePos + 2] <= "9"
                            ):
                                try:
                                    ev = intFn(tok[ePos + 1 :], 10)
                                    mv = intFn(tok[:ePos], 10)
                                    argsAppend(mv * (10**ev) if 0 <= ev < 300 else numDecFn(tok))
                                    continue
                                except ValueError:
                                    pass
                if decFull(tok) is not None:
                    argsAppend(numDecFn(tok))
                    continue
                ok = False
                break

            if c0 in ("+", "-"):
                if (
                    noUnder
                    and "." not in tok
                    and "e" not in tok
                    and "E" not in tok
                    and len(tok) > 1
                    and "0" <= tok[1] <= "9"
                    and (len(tok) < 3 or tok[2] not in ("x", "o", "b"))
                ):
                    try:
                        argsAppend(intFn(tok, 10))
                        continue
                    except ValueError:
                        pass
                if hexFull(tok) is not None:
                    try:
                        argsAppend(intFn(tok, 0))
                    except ValueError:
                        argsAppend(intFn(tok.replace("_", ""), 0))
                    continue
                if decFull(tok) is not None:
                    argsAppend(numDecFn(tok))
                    continue
                ok = False
                break

            if c0 >= "A":
                k, eq, pv = tok.partition("=")
                if eq and pv:
                    if k not in validIds:
                        if fastId(k) is None or k in keywords:
                            ok = False
                            break
                        validAdd(k)
                    av = atomsGet(pv, unset)
                    if av is not unset:
                        props[k] = av
                        continue
                    vc = pv[0]
                    if "1" <= vc <= "9" and noUnder:
                        if "." not in pv and "e" not in pv and "E" not in pv:
                            try:
                                props[k] = intFn(pv, 10)
                                continue
                            except ValueError:
                                pass
                        elif "." in pv:
                            dot = pv.find(".")
                            if dot + 1 < len(pv) and "0" <= pv[dot + 1] <= "9":
                                try:
                                    props[k] = floatFn(pv)
                                    continue
                                except ValueError:
                                    pass
                    elif vc == "0" and noUnder and len(pv) > 2:
                        pc1 = pv[1]
                        if pc1 == "x":
                            try:
                                props[k] = intFn(pv, 16)
                                continue
                            except ValueError:
                                pass
                        elif pc1 == "o":
                            try:
                                props[k] = intFn(pv, 8)
                                continue
                            except ValueError:
                                pass
                        elif pc1 == "b":
                            try:
                                props[k] = intFn(pv, 2)
                                continue
                            except ValueError:
                                pass
                        elif pc1 == "." and "0" <= pv[2] <= "9":
                            try:
                                props[k] = floatFn(pv)
                                continue
                            except ValueError:
                                pass
                    if hexFull(pv) is not None:
                        try:
                            props[k] = intFn(pv, 0)
                        except ValueError:
                            props[k] = intFn(pv.replace("_", ""), 0)
                        continue
                    if decFull(pv) is not None:
                        props[k] = numDecFn(pv)
                        continue
                ok = False
                break

            if c0 == "0":
                c1 = tok[1]
                if noUnder:
                    if c1 == "x":
                        try:
                            argsAppend(intFn(tok, 16))
                            continue
                        except ValueError:
                            pass
                    elif c1 == "o":
                        try:
                            argsAppend(intFn(tok, 8))
                            continue
                        except ValueError:
                            pass
                    elif c1 == "b":
                        try:
                            argsAppend(intFn(tok, 2))
                            continue
                        except ValueError:
                            pass
                    elif c1 == "." and len(tok) > 2 and "0" <= tok[2] <= "9":
                        try:
                            argsAppend(floatFn(tok))
                            continue
                        except ValueError:
                            pass
                if hexFull(tok) is not None:
                    try:
                        argsAppend(intFn(tok, 0))
                    except ValueError:
                        argsAppend(intFn(tok.replace("_", ""), 0))
                    continue
                if decFull(tok) is not None:
                    argsAppend(numDecFn(tok))
                    continue
                ok = False
                break

            if c0 == "/" and tok.startswith("//"):
                hasBrace = False
                break

            ok = False
            break

        if not ok:
            return (lineOffset, curNodes)

        if hasBrace:
            braceOff = lineOffset + rawLine.rfind("{")
            stackPush((curNodes, curAppend, p0, None, args, props, False, False, False, braceOff, -1, 0))
            curNodes = []
            curAppend = curNodes.append
        else:
            curAppend(nodeCls(p0, None, args, props, []))
        lineOffset += lineLen + 1

    return (endPos, curNodes)


def _tryLineCachedChunkNative(
    lines: list[str],
    pos: int,
    chunkEnd: int,
    *,
    consumeTrailingNl: bool,
    curNodes: list[Node],
    stack: list[_FastFrame],
    lineCache: dict[str, t.Any],
    tagCache: dict[str, t.Any],
    validIds: set[str],
    nativeTagged: bool,
) -> tuple[int, list[Node]]:
    """
    Adaptive line-template cached fast path for nativeUntaggedValues=True.
    `lines` comes from `chars[pos:chunkEnd].split("\\n")` with no CR/FF/NEL/LS/PS or `/*`.
    Returns `(newPos, curNodes)` — stops at exact `lineStart` on any line needing fallback,
    without mutating `curNodes` or `stack` for the failing line.
    """
    nodeCls = Node
    strCls = String
    valueCls = Value
    intFn = int
    floatFn = float
    atomsGet = _ATOMS.get
    keywords = _KEYWORDS
    unset = _UNSET
    unescFn = _unescapeValidated
    numDecFn = _numFromDecStr
    numOtherFn = _decodeNumOther
    convStrFn = _convertTaggedStringFast
    convDecFn = _convertTaggedDecFast
    decodeRawNumFn = _decodeRawNum
    headMatch = _LINE_HEAD_MATCH
    lineFindall = _LINE_TOK_FINDALL
    fastId = _fastIdFull

    lineGet = lineCache.get
    tagGet = tagCache.get
    validAdd = validIds.add

    curAppend = curNodes.append
    stackPush = stack.append
    stackPop = stack.pop

    nLines = len(lines)
    li = 0
    lineOffset = pos
    while li < nLines:
        rawLine = lines[li]
        lineStart = lineOffset
        lineLen = len(rawLine)
        li += 1
        lineOffset += lineLen + 1

        # lineCache entry shapes:
        #   0                              = empty or complete slashdashed line
        #   1                              = closing `}` line
        #   (0, name, tag, args, props)    = single leaf node
        #   (1, name, tag, args, props, braceCol) = `{` block opener
        #   (2, [(name, tag, args, props), ...])  = `;`-separated multi-node line
        #   (3, expectedNextLine, leafTuple)      = 2-line `\` continuation
        entry = lineGet(rawLine)
        if entry is not None:
            if entry == 0:
                continue
            if entry == 1:
                if not stack:
                    return (lineStart, curNodes)
                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, _, _, _, _ = stackPop()
                if not pND:
                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                curNodes = parentNodes
                curAppend = parentAppend
                continue
            et = entry[0]
            if et == 0:
                _, eName, eTag, eArgs, eProps = entry
                curAppend(nodeCls(eName, eTag, eArgs.copy() if eArgs else [], eProps.copy() if eProps else {}, []))
                continue
            if et == 1:
                _, eName, eTag, eArgs, eProps, braceCol = entry
                stackPush(
                    (
                        curNodes,
                        curAppend,
                        eName,
                        eTag,
                        eArgs.copy() if eArgs else [],
                        eProps.copy() if eProps else {},
                        False,
                        False,
                        False,
                        lineStart + braceCol,
                        -1,
                        0,
                    ),
                )
                curNodes = []
                curAppend = curNodes.append
                continue
            if et == 2:
                for eName, eTag, eArgs, eProps in entry[1]:
                    curAppend(nodeCls(eName, eTag, eArgs.copy() if eArgs else [], eProps.copy() if eProps else {}, []))
                continue
            if li < nLines and lines[li] == entry[1]:
                lineOffset += len(lines[li]) + 1
                li += 1
                _, eName, eTag, eArgs, eProps = entry[2]
                curAppend(nodeCls(eName, eTag, eArgs.copy() if eArgs else [], eProps.copy() if eProps else {}, []))
                continue

        stripped = rawLine.lstrip(" \t")
        if not stripped:
            if len(lineCache) < 512:
                lineCache[rawLine] = 0
            continue
        s0 = stripped[0]
        if s0 == "}":
            if len(stripped) == 1:
                if not stack:
                    return (lineStart, curNodes)
                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, _, _, _, _ = stackPop()
                if not pND:
                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                curNodes = parentNodes
                curAppend = parentAppend
                if len(lineCache) < 512:
                    lineCache[rawLine] = 1
                continue
            return (lineStart, curNodes)
        if s0 == "/" and len(stripped) > 1 and stripped[1] == "/":
            continue

        nextLineForCont: str | None = None
        extraLineLen = 0
        if stripped[-1] == "\\":
            if li >= nLines:
                return (lineStart, curNodes)
            nextLineForCont = lines[li]
            stripped = stripped + "\n" + nextLineForCont
            extraLineLen = len(nextLineForCont) + 1
        cacheable = True

        sd: str | None = None
        nTag: str | None = None
        if "A" <= s0 <= "z" or s0 == "_":
            sp = stripped.find(" ")
            if sp == -1:
                if stripped in validIds:
                    curAppend(nodeCls(stripped, None, [], {}, []))
                    if len(lineCache) < 512:
                        lineCache[rawLine] = (0, stripped, None, None, None)
                    continue
                if fastId(stripped) is not None and stripped not in keywords:
                    validAdd(stripped)
                    curAppend(nodeCls(stripped, None, [], {}, []))
                    if len(lineCache) < 512:
                        lineCache[rawLine] = (0, stripped, None, None, None)
                    continue
                mh = headMatch(stripped, 0)
                if mh is None:
                    return (lineStart, curNodes)
                sd, nTag, nName = mh.groups()
                if nName in keywords or (nTag and nTag in keywords):
                    return (lineStart, curNodes)
                tokPos = mh.end()
            else:
                nName = stripped[:sp]
                if nName in validIds:
                    tokPos = sp
                elif fastId(nName) is not None and nName not in keywords:
                    validAdd(nName)
                    tokPos = sp
                else:
                    mh = headMatch(stripped, 0)
                    if mh is None:
                        return (lineStart, curNodes)
                    sd, nTag, nName = mh.groups()
                    if nName in keywords or (nTag and nTag in keywords):
                        return (lineStart, curNodes)
                    tokPos = mh.end()
        else:
            mh = headMatch(stripped, 0)
            if mh is None:
                return (lineStart, curNodes)
            sd, nTag, nName = mh.groups()
            if nName in keywords or (nTag and nTag in keywords):
                return (lineStart, curNodes)
            tokPos = mh.end()

        toks = lineFindall(stripped, tokPos)
        if toks and toks[-1][5] and toks[-1][5] not in (";", "{"):
            if nextLineForCont is None and '"' in toks[-1][5] and li < nLines:
                nextLineMl = lines[li]
                toks = lineFindall(stripped + "\n" + nextLineMl, tokPos)
                if toks and toks[-1][5] and toks[-1][5] not in (";", "{"):
                    return (lineStart, curNodes)
                extraLineLen = len(nextLineMl) + 1
                cacheable = False
            else:
                return (lineStart, curNodes)

        args: list[t.Any] = []
        argsAppend = args.append
        props: dict[str, t.Any] = {}
        nodeDiscard = sd is not None
        nextEntDiscard = False
        discDepth = 0
        opened = False
        hasMutable = False
        lineNodes: list[Node] | None = None
        emitted: list[tuple[str, str | None, list[t.Any] | None, dict[str, t.Any] | None]] | None = None
        state = 1
        curName = nName
        curTag = nTag
        ok = True

        for k, pstr, pv, astr, arg, fb in toks:
            if k:
                if opened or state != 1 or k in keywords:
                    ok = False
                    break
                if nodeDiscard or nextEntDiscard:
                    nextEntDiscard = False
                    if pv and pv[0] == '"':
                        try:
                            unescFn(pv[1:-1])
                        except ValueError:
                            ok = False
                            break
                    continue
                if not pv:
                    props[k] = pstr
                    continue
                av = atomsGet(pv, unset)
                if av is not unset:
                    props[k] = av
                    continue
                vc = pv[0]
                if "1" <= vc <= "9":
                    if "." not in pv and "e" not in pv and "E" not in pv:
                        try:
                            props[k] = intFn(pv, 10)
                        except ValueError:
                            props[k] = numDecFn(pv)
                    elif "." in pv:
                        try:
                            props[k] = floatFn(pv)
                        except ValueError:
                            props[k] = floatFn(pv.replace("_", ""))
                    else:
                        props[k] = numOtherFn(pv)
                elif vc == '"':
                    try:
                        props[k] = unescFn(pv[1:-1])
                    except ValueError:
                        ok = False
                        break
                else:
                    props[k] = numOtherFn(pv)
                continue

            if astr:
                if opened or state != 1:
                    ok = False
                    break
                entDisc = nodeDiscard or nextEntDiscard
                nextEntDiscard = False
                if not entDisc:
                    argsAppend(astr)
                continue

            if arg:
                if opened:
                    ok = False
                    break
                ac0 = arg[0]
                if state != 1:
                    if state == 0:
                        if ac0 == "}":
                            if discDepth <= 0:
                                ok = False
                                break
                            discDepth -= 1
                            state = 2
                            continue
                        if (
                            ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                            and not ("0" <= ac0 <= "9")
                            and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                            and arg not in keywords
                        ):
                            curName = arg
                            curTag = None
                            args = []
                            argsAppend = args.append
                            props = {}
                            state = 1
                            continue
                    ok = False
                    break
                if ac0 == "{":
                    if nodeDiscard:
                        nextEntDiscard = False
                        discDepth += 1
                        state = 0
                        continue
                    if nextEntDiscard:
                        ok = False
                        break
                    opened = True
                    state = 0
                    continue
                if ac0 == "/":
                    if arg[1] == "-":
                        if nextEntDiscard:
                            ok = False
                            break
                        nextEntDiscard = True
                        continue
                    ok = False
                    break
                if ac0 == "\\":
                    continue
                if ac0 == "}":
                    ok = False
                    break
                entDisc = nodeDiscard or nextEntDiscard
                nextEntDiscard = False
                if "1" <= ac0 <= "9":
                    if not entDisc:
                        if "." not in arg and "e" not in arg and "E" not in arg:
                            try:
                                argsAppend(intFn(arg, 10))
                            except ValueError:
                                argsAppend(numDecFn(arg))
                        elif "." in arg:
                            try:
                                argsAppend(floatFn(arg))
                            except ValueError:
                                argsAppend(floatFn(arg.replace("_", "")))
                        else:
                            argsAppend(numOtherFn(arg))
                    continue
                if ac0 == '"':
                    inner = arg[1:-1]
                    if "\\" in inner:
                        try:
                            inner = unescFn(inner)
                        except ValueError:
                            ok = False
                            break
                    if not entDisc:
                        argsAppend(inner)
                    continue
                if ac0 == "(":
                    rp = arg.find(")", 1)
                    tName = arg[1:rp]
                    if tName in keywords:
                        ok = False
                        break
                    vStr = arg[rp + 1 :]
                    if vStr[0] == '"':
                        inner = vStr[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                ok = False
                                break
                        if not entDisc:
                            if nativeTagged:
                                cv = tagGet(arg, unset)
                                if cv is not unset:
                                    argsAppend(cv)
                                    continue
                                conv = convStrFn(tName, inner)
                                if conv is unset:
                                    ok = False
                                    break
                                if isinstance(conv, valueCls):
                                    hasMutable = True
                                elif len(tagCache) < 1024:
                                    tagCache[arg] = conv
                                argsAppend(conv)
                            else:
                                hasMutable = True
                                argsAppend(strCls(inner, tName))
                    elif not entDisc:
                        if nativeTagged:
                            cv = tagGet(arg, unset)
                            if cv is not unset:
                                argsAppend(cv)
                                continue
                            conv = convDecFn(tName, vStr)
                            if conv is unset:
                                ok = False
                                break
                            if isinstance(conv, valueCls):
                                hasMutable = True
                            elif len(tagCache) < 1024:
                                tagCache[arg] = conv
                            argsAppend(conv)
                        else:
                            hasMutable = True
                            dObj = decodeRawNumFn(vStr)
                            dObj.tag = tName
                            argsAppend(dObj)
                    continue
                if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                    if not entDisc:
                        q = arg.find('"', 1)
                        argsAppend(arg[q + 1 : len(arg) - q])
                    continue
                av = atomsGet(arg, unset)
                if av is not unset:
                    if not entDisc:
                        argsAppend(av)
                    continue
                if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                    if not entDisc:
                        argsAppend(numOtherFn(arg))
                    continue
                ok = False
                break

            if fb:
                if opened:
                    ok = False
                    break
                if fb == ";":
                    if nextEntDiscard or state == 0:
                        ok = False
                        break
                    if nodeDiscard:
                        if discDepth == 0:
                            ok = False
                            break
                        state = 0
                        continue
                    if state == 1:
                        built = nodeCls(curName, curTag, args, props, [])
                        rec = (curName, curTag, args.copy() if args else None, props.copy() if props else None)
                        if lineNodes is None:
                            lineNodes = [built]
                            emitted = [rec]
                        else:
                            lineNodes.append(built)
                            assert emitted is not None
                            emitted.append(rec)
                    state = 0
                    continue
                if fb == "{" and state == 1:
                    if nodeDiscard:
                        nextEntDiscard = False
                        discDepth += 1
                        state = 0
                        continue
                    if nextEntDiscard:
                        ok = False
                        break
                    opened = True
                    state = 0
                    continue
                ok = False
                break

        if not ok or nextEntDiscard or discDepth != 0:
            return (lineStart, curNodes)

        if extraLineLen:
            li += 1
            lineOffset += extraLineLen

        if lineNodes is not None:
            curNodes.extend(lineNodes)

        if opened:
            cPos = rawLine.find("//")
            braceCol = rawLine.rfind("{", 0, cPos if cPos != -1 else lineLen)
            stackPush(
                (
                    curNodes,
                    curAppend,
                    curName,
                    curTag,
                    args,
                    props,
                    False,
                    False,
                    False,
                    lineStart + braceCol,
                    -1,
                    0,
                ),
            )
            curNodes = []
            curAppend = curNodes.append
            if cacheable and not hasMutable and nextLineForCont is None and lineNodes is None and len(lineCache) < 512:
                lineCache[rawLine] = (
                    1,
                    curName,
                    curTag,
                    args.copy() if args else None,
                    props.copy() if props else None,
                    braceCol,
                )
        elif state == 1:
            if not nodeDiscard:
                curAppend(nodeCls(curName, curTag, args, props, []))
                if cacheable and not hasMutable and len(lineCache) < 512:
                    if nextLineForCont is not None and lineNodes is None:
                        lineCache[rawLine] = (
                            3,
                            nextLineForCont,
                            (0, curName, curTag, args.copy() if args else None, props.copy() if props else None),
                        )
                    elif nextLineForCont is None:
                        if emitted is None:
                            lineCache[rawLine] = (
                                0,
                                curName,
                                curTag,
                                args.copy() if args else None,
                                props.copy() if props else None,
                            )
                        else:
                            emitted.append(
                                (curName, curTag, args.copy() if args else None, props.copy() if props else None),
                            )
                            lineCache[rawLine] = (2, emitted)
            elif cacheable and nextLineForCont is None and lineNodes is None and len(lineCache) < 512:
                lineCache[rawLine] = 0
        elif cacheable and not hasMutable and nextLineForCont is None and len(lineCache) < 512:
            lineCache[rawLine] = 0 if emitted is None else (2, emitted)

    return (chunkEnd + 1 if consumeTrailingNl else chunkEnd, curNodes)


def _tryLineCachedChunkRaw(
    lines: list[str],
    pos: int,
    chunkEnd: int,
    *,
    consumeTrailingNl: bool,
    curNodes: list[Node],
    stack: list[_FastFrame],
    lineCache: dict[str, t.Any],
    validIds: set[str],
) -> tuple[int, list[Node]]:
    """
    Adaptive line-template cached fast path for raw mode (`nativeUntaggedValues=False, nativeTaggedValues=False`).
    Stores immutable constructor recipes so each cache hit instantiates fresh `Value` objects with zero sharing.
    """
    nodeCls = Node
    strCls = String
    rawStrCls = RawString
    boolCls = Bool
    nullCls = Null
    decCls = Decimal
    intFn = int
    floatFn = float
    keywords = _KEYWORDS
    unescFn = _unescapeValidated
    decodeRawNumFn = _decodeRawNum
    headMatch = _LINE_HEAD_MATCH
    lineFindall = _LINE_TOK_FINDALL
    fastId = _fastIdFull

    lineGet = lineCache.get
    validAdd = validIds.add

    curAppend = curNodes.append
    stackPush = stack.append
    stackPop = stack.pop

    nLines = len(lines)
    li = 0
    lineOffset = pos
    while li < nLines:
        rawLine = lines[li]
        lineStart = lineOffset
        lineLen = len(rawLine)
        li += 1
        lineOffset += lineLen + 1

        # Raw lineCache entry shapes (all recipes instantiate fresh `Value` objects on replay):
        #   0                                         = empty or complete slashdashed line
        #   1                                         = closing `}` line
        #   (0, name, tag, argRecs, propRecs)         = fast 1-arg leaf (`(cls, val)` / `(pk, cls, val)`)
        #   (1, name, tag, argRecs, propRecs, braceCol) = fast 1-arg `{` block opener
        #   (2, [(name, tag, argRecs, propRecs), ...])  = fast 1-arg `;`-separated multi-node line
        #   (3, expectedNextLine, leafRecipe)         = fast 1-arg 2-line `\` continuation
        #   (4, name, tag, argRecs, propRecs, braceCol) = general recipe (tagged or non-zero-exp Decimal; braceCol < 0 for leaf)
        entry = lineGet(rawLine)
        if entry is not None:
            if entry == 0:
                continue
            if entry == 1:
                if not stack:
                    return (lineStart, curNodes)
                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, _, _, _, _ = stackPop()
                if not pND:
                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                curNodes = parentNodes
                curAppend = parentAppend
                continue
            et = entry[0]
            if et == 0:
                _, eName, eTag, eArgs, eProps = entry
                rArgs = [cls(a) for cls, a in eArgs] if eArgs else []
                rProps = {pk: cls(a) for pk, cls, a in eProps} if eProps else {}
                curAppend(nodeCls(eName, eTag, rArgs, rProps, []))
                continue
            if et == 1:
                _, eName, eTag, eArgs, eProps, braceCol = entry
                rArgs = [cls(a) for cls, a in eArgs] if eArgs else []
                rProps = {pk: cls(a) for pk, cls, a in eProps} if eProps else {}
                stackPush(
                    (
                        curNodes,
                        curAppend,
                        eName,
                        eTag,
                        rArgs,
                        rProps,
                        False,
                        False,
                        False,
                        lineStart + braceCol,
                        -1,
                        0,
                    ),
                )
                curNodes = []
                curAppend = curNodes.append
                continue
            if et == 2:
                for eName, eTag, eArgs, eProps in entry[1]:
                    rArgs = [cls(a) for cls, a in eArgs] if eArgs else []
                    rProps = {pk: cls(a) for pk, cls, a in eProps} if eProps else {}
                    curAppend(nodeCls(eName, eTag, rArgs, rProps, []))
                continue
            if et == 3:
                if li < nLines and lines[li] == entry[1]:
                    lineOffset += len(lines[li]) + 1
                    li += 1
                    _, eName, eTag, eArgs, eProps = entry[2]
                    rArgs = [cls(a) for cls, a in eArgs] if eArgs else []
                    rProps = {pk: cls(a) for pk, cls, a in eProps} if eProps else {}
                    curAppend(nodeCls(eName, eTag, rArgs, rProps, []))
                    continue
            else:
                # General recipe (contains non-zero-exponent Decimal or tagged values)
                _, eName, eTag, eArgs, eProps, braceCol = entry
                rArgs = (
                    [
                        (
                            it[0](it[1])
                            if len(it) == 2
                            else (it[0](it[1], it[2]) if len(it) == 3 else decCls(it[1], it[2], it[3]))
                        )
                        for it in eArgs
                    ]
                    if eArgs
                    else []
                )
                rProps = (
                    {it[0]: (it[1](it[2]) if len(it) == 3 else it[1](it[2], it[3])) for it in eProps} if eProps else {}
                )
                if braceCol < 0:
                    curAppend(nodeCls(eName, eTag, rArgs, rProps, []))
                else:
                    stackPush(
                        (
                            curNodes,
                            curAppend,
                            eName,
                            eTag,
                            rArgs,
                            rProps,
                            False,
                            False,
                            False,
                            lineStart + braceCol,
                            -1,
                            0,
                        ),
                    )
                    curNodes = []
                    curAppend = curNodes.append
                continue

        stripped = rawLine.lstrip(" \t")
        if not stripped:
            if len(lineCache) < 512:
                lineCache[rawLine] = 0
            continue
        s0 = stripped[0]
        if s0 == "}":
            if len(stripped) == 1:
                if not stack:
                    return (lineStart, curNodes)
                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, _, _, _, _ = stackPop()
                if not pND:
                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                curNodes = parentNodes
                curAppend = parentAppend
                if len(lineCache) < 512:
                    lineCache[rawLine] = 1
                continue
            return (lineStart, curNodes)
        if s0 == "/" and len(stripped) > 1 and stripped[1] == "/":
            continue

        nextLineForCont: str | None = None
        extraLineLen = 0
        if stripped[-1] == "\\":
            if li >= nLines:
                return (lineStart, curNodes)
            nextLineForCont = lines[li]
            stripped = stripped + "\n" + nextLineForCont
            extraLineLen = len(nextLineForCont) + 1
        cacheable = True

        sd: str | None = None
        nTag: str | None = None
        if "A" <= s0 <= "z" or s0 == "_":
            sp = stripped.find(" ")
            if sp == -1:
                if stripped in validIds:
                    curAppend(nodeCls(stripped, None, [], {}, []))
                    if len(lineCache) < 512:
                        lineCache[rawLine] = (0, stripped, None, None, None)
                    continue
                if fastId(stripped) is not None and stripped not in keywords:
                    validAdd(stripped)
                    curAppend(nodeCls(stripped, None, [], {}, []))
                    if len(lineCache) < 512:
                        lineCache[rawLine] = (0, stripped, None, None, None)
                    continue
                mh = headMatch(stripped, 0)
                if mh is None:
                    return (lineStart, curNodes)
                sd, nTag, nName = mh.groups()
                if nName in keywords or (nTag and nTag in keywords):
                    return (lineStart, curNodes)
                tokPos = mh.end()
            else:
                nName = stripped[:sp]
                if nName in validIds:
                    tokPos = sp
                elif fastId(nName) is not None and nName not in keywords:
                    validAdd(nName)
                    tokPos = sp
                else:
                    mh = headMatch(stripped, 0)
                    if mh is None:
                        return (lineStart, curNodes)
                    sd, nTag, nName = mh.groups()
                    if nName in keywords or (nTag and nTag in keywords):
                        return (lineStart, curNodes)
                    tokPos = mh.end()
        else:
            mh = headMatch(stripped, 0)
            if mh is None:
                return (lineStart, curNodes)
            sd, nTag, nName = mh.groups()
            if nName in keywords or (nTag and nTag in keywords):
                return (lineStart, curNodes)
            tokPos = mh.end()

        toks = lineFindall(stripped, tokPos)
        if toks and toks[-1][5] and toks[-1][5] not in (";", "{"):
            if nextLineForCont is None and '"' in toks[-1][5] and li < nLines:
                nextLineMl = lines[li]
                toks = lineFindall(stripped + "\n" + nextLineMl, tokPos)
                if toks and toks[-1][5] and toks[-1][5] not in (";", "{"):
                    return (lineStart, curNodes)
                extraLineLen = len(nextLineMl) + 1
                cacheable = False
            else:
                return (lineStart, curNodes)

        args: list[t.Any] = []
        argsAppend = args.append
        argRecs: list[tuple[t.Any, ...]] = []
        props: dict[str, t.Any] = {}
        propRecs: dict[str, tuple[t.Any, ...]] = {}
        fastRec = True
        nodeDiscard = sd is not None
        nextEntDiscard = False
        discDepth = 0
        opened = False
        lineNodes: list[Node] | None = None
        emitted: list[tuple[t.Any, ...]] | None = None
        state = 1
        curName = nName
        curTag = nTag
        ok = True

        for k, pstr, pv, astr, arg, fb in toks:
            if k:
                if opened or state != 1 or k in keywords:
                    ok = False
                    break
                if nodeDiscard or nextEntDiscard:
                    nextEntDiscard = False
                    if pv and pv[0] == '"':
                        try:
                            unescFn(pv[1:-1])
                        except ValueError:
                            ok = False
                            break
                    continue
                if not pv:
                    props[k] = strCls(pstr)
                    propRecs[k] = (k, strCls, pstr)
                    continue
                vc = pv[0]
                if "1" <= vc <= "9":
                    if "." not in pv and "e" not in pv and "E" not in pv:
                        try:
                            iv = intFn(pv, 10)
                            props[k] = decCls(iv, 0)
                            propRecs[k] = (k, decCls, iv)
                            continue
                        except ValueError:
                            pass
                    elif "e" not in pv and "E" not in pv:
                        try:
                            fv = floatFn(pv)
                        except ValueError:
                            fv = floatFn(pv.replace("_", ""))
                        props[k] = decCls(fv, 0)
                        propRecs[k] = (k, decCls, fv)
                        continue
                    vObj = decodeRawNumFn(pv)
                    props[k] = vObj
                    if isinstance(vObj, decCls):
                        if vObj.exponent == 0:
                            propRecs[k] = (k, decCls, vObj.mantissa)
                        else:
                            fastRec = False
                            propRecs[k] = (k, decCls, vObj.mantissa, vObj.exponent)
                    else:
                        propRecs[k] = (k, type(vObj), vObj.value)
                    continue
                if vc in ("t", "f", "n"):
                    if pv == "true":
                        props[k] = boolCls(True)
                        propRecs[k] = (k, boolCls, True)
                    elif pv == "false":
                        props[k] = boolCls(False)
                        propRecs[k] = (k, boolCls, False)
                    else:
                        props[k] = nullCls()
                        propRecs[k] = (k, nullCls, None)
                    continue
                if vc == '"':
                    try:
                        u = unescFn(pv[1:-1])
                    except ValueError:
                        ok = False
                        break
                    props[k] = strCls(u)
                    propRecs[k] = (k, strCls, u)
                    continue
                vObj = decodeRawNumFn(pv)
                props[k] = vObj
                if isinstance(vObj, decCls):
                    if vObj.exponent == 0:
                        propRecs[k] = (k, decCls, vObj.mantissa)
                    else:
                        fastRec = False
                        propRecs[k] = (k, decCls, vObj.mantissa, vObj.exponent)
                else:
                    propRecs[k] = (k, type(vObj), vObj.value)
                continue

            if astr:
                if opened or state != 1:
                    ok = False
                    break
                entDisc = nodeDiscard or nextEntDiscard
                nextEntDiscard = False
                if not entDisc:
                    argsAppend(strCls(astr))
                    argRecs.append((strCls, astr))
                continue

            if arg:
                if opened:
                    ok = False
                    break
                ac0 = arg[0]
                if state != 1:
                    if state == 0:
                        if ac0 == "}":
                            if discDepth <= 0:
                                ok = False
                                break
                            discDepth -= 1
                            state = 2
                            continue
                        if (
                            ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                            and not ("0" <= ac0 <= "9")
                            and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                            and arg not in keywords
                        ):
                            curName = arg
                            curTag = None
                            args = []
                            argsAppend = args.append
                            argRecs = []
                            props = {}
                            propRecs = {}
                            state = 1
                            continue
                    ok = False
                    break
                if ac0 == "{":
                    if nodeDiscard:
                        nextEntDiscard = False
                        discDepth += 1
                        state = 0
                        continue
                    if nextEntDiscard:
                        ok = False
                        break
                    opened = True
                    state = 0
                    continue
                if ac0 == "/":
                    if arg[1] == "-":
                        if nextEntDiscard:
                            ok = False
                            break
                        nextEntDiscard = True
                        continue
                    ok = False
                    break
                if ac0 == "\\":
                    continue
                if ac0 == "}":
                    ok = False
                    break
                entDisc = nodeDiscard or nextEntDiscard
                nextEntDiscard = False
                if "1" <= ac0 <= "9":
                    if not entDisc:
                        if "." not in arg and "e" not in arg and "E" not in arg:
                            try:
                                iv = intFn(arg, 10)
                                argsAppend(decCls(iv, 0))
                                argRecs.append((decCls, iv))
                                continue
                            except ValueError:
                                pass
                        elif "e" not in arg and "E" not in arg:
                            try:
                                fv = floatFn(arg)
                            except ValueError:
                                fv = floatFn(arg.replace("_", ""))
                            argsAppend(decCls(fv, 0))
                            argRecs.append((decCls, fv))
                            continue
                        vObj = decodeRawNumFn(arg)
                        argsAppend(vObj)
                        if isinstance(vObj, decCls):
                            if vObj.exponent == 0:
                                argRecs.append((decCls, vObj.mantissa))
                            else:
                                fastRec = False
                                argRecs.append((decCls, vObj.mantissa, vObj.exponent))
                        else:
                            argRecs.append((type(vObj), vObj.value))
                    continue
                if ac0 == '"':
                    inner = arg[1:-1]
                    if "\\" in inner:
                        try:
                            inner = unescFn(inner)
                        except ValueError:
                            ok = False
                            break
                    if not entDisc:
                        argsAppend(strCls(inner))
                        argRecs.append((strCls, inner))
                    continue
                if ac0 == "(":
                    rp = arg.find(")", 1)
                    tName = arg[1:rp]
                    if tName in keywords:
                        ok = False
                        break
                    vStr = arg[rp + 1 :]
                    if vStr[0] == '"':
                        inner = vStr[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                ok = False
                                break
                        if not entDisc:
                            fastRec = False
                            argsAppend(strCls(inner, tName))
                            argRecs.append((strCls, inner, tName))
                    elif not entDisc:
                        fastRec = False
                        vObj = decodeRawNumFn(vStr)
                        vObj.tag = tName
                        argsAppend(vObj)
                        if isinstance(vObj, decCls):
                            argRecs.append((decCls, vObj.mantissa, vObj.exponent, tName))
                        else:
                            argRecs.append((type(vObj), vObj.value, tName))
                    continue
                if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                    if not entDisc:
                        q = arg.find('"', 1)
                        rs = arg[q + 1 : len(arg) - q]
                        argsAppend(rawStrCls(rs))
                        argRecs.append((rawStrCls, rs))
                    continue
                if arg in keywords:
                    if not entDisc:
                        if arg == "true":
                            argsAppend(boolCls(True))
                            argRecs.append((boolCls, True))
                        elif arg == "false":
                            argsAppend(boolCls(False))
                            argRecs.append((boolCls, False))
                        else:
                            argsAppend(nullCls())
                            argRecs.append((nullCls, None))
                    continue
                if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                    if not entDisc:
                        vObj = decodeRawNumFn(arg)
                        argsAppend(vObj)
                        if isinstance(vObj, decCls):
                            if vObj.exponent == 0:
                                argRecs.append((decCls, vObj.mantissa))
                            else:
                                fastRec = False
                                argRecs.append((decCls, vObj.mantissa, vObj.exponent))
                        else:
                            argRecs.append((type(vObj), vObj.value))
                    continue
                ok = False
                break

            if fb:
                if opened:
                    ok = False
                    break
                if fb == ";":
                    if nextEntDiscard or state == 0:
                        ok = False
                        break
                    if nodeDiscard:
                        if discDepth == 0:
                            ok = False
                            break
                        state = 0
                        continue
                    if state == 1:
                        built = nodeCls(curName, curTag, args, props, [])
                        if fastRec:
                            rec = (
                                curName,
                                curTag,
                                tuple(argRecs) if argRecs else None,
                                tuple(propRecs.values()) if propRecs else None,
                            )
                        else:
                            cacheable = False
                            rec = (curName, curTag, None, None)
                        if lineNodes is None:
                            lineNodes = [built]
                            emitted = [rec]
                        else:
                            lineNodes.append(built)
                            assert emitted is not None
                            emitted.append(rec)
                    state = 0
                    continue
                if fb == "{" and state == 1:
                    if nodeDiscard:
                        nextEntDiscard = False
                        discDepth += 1
                        state = 0
                        continue
                    if nextEntDiscard:
                        ok = False
                        break
                    opened = True
                    state = 0
                    continue
                ok = False
                break

        if not ok or nextEntDiscard or discDepth != 0:
            return (lineStart, curNodes)

        if extraLineLen:
            li += 1
            lineOffset += extraLineLen

        if lineNodes is not None:
            curNodes.extend(lineNodes)

        if opened:
            cPos = rawLine.find("//")
            braceCol = rawLine.rfind("{", 0, cPos if cPos != -1 else lineLen)
            stackPush(
                (
                    curNodes,
                    curAppend,
                    curName,
                    curTag,
                    args,
                    props,
                    False,
                    False,
                    False,
                    lineStart + braceCol,
                    -1,
                    0,
                ),
            )
            curNodes = []
            curAppend = curNodes.append
            if cacheable and nextLineForCont is None and lineNodes is None and len(lineCache) < 512:
                lineCache[rawLine] = (
                    1 if fastRec else 4,
                    curName,
                    curTag,
                    tuple(argRecs) if argRecs else None,
                    tuple(propRecs.values()) if propRecs else None,
                    braceCol,
                )
        elif state == 1:
            if not nodeDiscard:
                curAppend(nodeCls(curName, curTag, args, props, []))
                if cacheable and len(lineCache) < 512:
                    recA = tuple(argRecs) if argRecs else None
                    recP = tuple(propRecs.values()) if propRecs else None
                    if fastRec:
                        if nextLineForCont is not None and lineNodes is None:
                            lineCache[rawLine] = (3, nextLineForCont, (0, curName, curTag, recA, recP))
                        elif nextLineForCont is None:
                            if emitted is None:
                                lineCache[rawLine] = (0, curName, curTag, recA, recP)
                            else:
                                emitted.append((curName, curTag, recA, recP))
                                lineCache[rawLine] = (2, emitted)
                    elif nextLineForCont is None and emitted is None:
                        lineCache[rawLine] = (4, curName, curTag, recA, recP, -1)
            elif cacheable and nextLineForCont is None and lineNodes is None and len(lineCache) < 512:
                lineCache[rawLine] = 0
        elif cacheable and nextLineForCont is None and len(lineCache) < 512:
            lineCache[rawLine] = 0 if emitted is None else (2, emitted)

    return (chunkEnd + 1 if consumeTrailingNl else chunkEnd, curNodes)


def _parseChunkedNative(chars: str, config: t.ParseConfig) -> Document:
    """Specialized chunked C-regex parser for nativeUntaggedValues=True with no custom converters."""
    n = len(chars)
    nativeTagged = config.nativeTaggedValues
    findall = _tokFindall
    lengthHint = _lengthHint
    atomsGet = _ATOMS.get
    keywords = _KEYWORDS
    unset = _UNSET
    nodeCls = Node
    strCls = String
    valueCls = Value
    floatFn = float
    intFn = int
    unescFn = _unescapeValidated
    numDecFn = _numFromDecStr
    numOtherFn = _decodeNumOther
    convStrFn = _convertTaggedStringFast
    convDecFn = _convertTaggedDecFast
    decodeRawNumFn = _decodeRawNum

    s: Stream | None = None
    curNodes: list[Node] = []
    curAppend = curNodes.append
    curDiscard = False
    stack: list[_FastFrame] = []
    stackPush = stack.append
    stackPop = stack.pop
    lineCache: dict[str, t.Any] = {}
    tagCache: dict[str, t.Any] = {}
    tagGet = tagCache.get
    validIds: set[str] = set()

    state = 0
    name = ""
    tag: str | None = None
    args: list[t.Any] = []
    argsAppend = args.append
    props: dict[str, t.Any] = {}
    nodeDiscard = False
    nextEntDiscard = False
    sdTokIdx = 0

    pos = 0
    noQuoteDoc = (
        n >= 256
        and chars.isascii()
        and '"' not in chars
        and ";" not in chars
        and "/*" not in chars
        and "/-" not in chars
        and "\\" not in chars
        and "(" not in chars
        and "\r" not in chars
        and "\x0c" not in chars
        and "\x0b" not in chars
    )

    chunkSize = 4096 if n >= 2048 else 512
    while pos < n:
        endPos = _pickChunkEnd(chars, pos, n, chunkSize)
        if endPos < 0:
            if s is None:
                s = Stream(chars, config)
            (
                newPos,
                state,
                name,
                tag,
                args,
                props,
                nodeDiscard,
                curNodes,
                curDiscard,
            ) = _handleChunkFallback(
                s,
                pos,
                nativeUntagged=True,
                nativeTagged=nativeTagged,
                state=state,
                name=name,
                tag=tag,
                args=args,
                props=props,
                nodeDiscard=nodeDiscard,
                curNodes=curNodes,
                curDiscard=curDiscard,
                stack=stack,
            )
            if newPos < 0:
                return Document(curNodes)
            curAppend = curNodes.append
            argsAppend = args.append
            pos = newPos
            continue

        # Tier 1: Quote-free pure-ASCII line-split path (skips regex completely for numeric/property chunks).
        if noQuoteDoc and state == 0 and not curDiscard and endPos - pos >= 256:
            qfEnd = endPos + 1 if endPos < n and chars[endPos] == "\n" else endPos
            qfPos, curNodes = _tryQuoteFreeChunkNative(
                chars,
                pos,
                qfEnd,
                curNodes=curNodes,
                stack=stack,
                validIds=validIds,
            )
            curAppend = curNodes.append
            if qfPos >= qfEnd:
                pos = qfPos
                chunkSize = min(chunkSize * 4, 32768)
                continue
            noQuoteDoc = False
            if s is None:
                s = Stream(chars, config)
            (
                newPos,
                state,
                name,
                tag,
                args,
                props,
                nodeDiscard,
                curNodes,
                curDiscard,
            ) = _handleChunkFallback(
                s,
                qfPos,
                nativeUntagged=True,
                nativeTagged=nativeTagged,
                state=0,
                name="",
                tag=None,
                args=[],
                props={},
                nodeDiscard=False,
                curNodes=curNodes,
                curDiscard=False,
                stack=stack,
            )
            if newPos < 0:
                return Document(curNodes)
            curAppend = curNodes.append
            argsAppend = args.append
            pos = newPos
            continue

        # Tier 2: Adaptive line-template cache for chunks with repeating lines.
        if state == 0 and not curDiscard and not nodeDiscard and not nextEntDiscard and endPos - pos >= 512:
            lcEnd = endPos
            consumeNl = lcEnd < n and chars[lcEnd] == "\n"
            if not consumeNl and lcEnd < n:
                lastNl = chars.rfind("\n", pos, lcEnd)
                if lastNl > pos:
                    lcEnd = lastNl
                    consumeNl = True
            if lcEnd - pos >= 512:
                chunkSlice = chars[pos:lcEnd]
                if (
                    "\r" not in chunkSlice
                    and "/*" not in chunkSlice
                    and "\x0c" not in chunkSlice
                    and (
                        chunkSlice.isascii()
                        or ("\x85" not in chunkSlice and "\u2028" not in chunkSlice and "\u2029" not in chunkSlice)
                    )
                ):
                    chunkLines = chunkSlice.split("\n")
                    if lineCache or _hasRepeatingLines(chunkLines):
                        lcPos, curNodes = _tryLineCachedChunkNative(
                            chunkLines,
                            pos,
                            lcEnd,
                            consumeTrailingNl=consumeNl,
                            curNodes=curNodes,
                            stack=stack,
                            lineCache=lineCache,
                            tagCache=tagCache,
                            validIds=validIds,
                            nativeTagged=nativeTagged,
                        )
                        curAppend = curNodes.append
                        targetPos = lcEnd + 1 if consumeNl else lcEnd
                        if lcPos >= targetPos:
                            pos = lcPos
                            chunkSize = min(chunkSize * 4, 65536)
                            continue
                        if s is None:
                            s = Stream(chars, config)
                        (
                            newPos,
                            state,
                            name,
                            tag,
                            args,
                            props,
                            nodeDiscard,
                            curNodes,
                            curDiscard,
                        ) = _handleChunkFallback(
                            s,
                            lcPos,
                            nativeUntagged=True,
                            nativeTagged=nativeTagged,
                            state=0,
                            name="",
                            tag=None,
                            args=[],
                            props={},
                            nodeDiscard=False,
                            curNodes=curNodes,
                            curDiscard=False,
                            stack=stack,
                        )
                        if newPos < 0:
                            return Document(curNodes)
                        curAppend = curNodes.append
                        argsAppend = args.append
                        pos = newPos
                        chunkSize = max(512, chunkSize // 2)
                        continue

        # Tier 3: Prime byte 0 bare node head so 7-group tokenizer starts at state == 1 with zero fallback.
        if pos == 0 and state == 0:
            mh0 = _matchNodeHead(chars, 0)
            assert mh0 is not None
            li0 = mh0.lastindex
            if li0 == 1:
                cand0 = mh0.group(1)
                if cand0 not in keywords:
                    name = cand0
                    pos = mh0.end()
                    state = 1
                    if pos >= endPos:
                        continue
            elif li0 is None and mh0.end() >= n:
                return Document(curNodes)

        # Parser state: 0 = expecting node head or `}`, 1 = inside node (entries / `{` / terminator),
        # 2 = immediately after `}` (expecting node terminator).
        # `nToks - 1 - lengthHint(tokIter)` recovers the current token index on rare fallback without `enumerate()`.
        toks = findall(chars, pos, endPos)
        nToks = len(toks)
        tokIter = iter(toks)
        failIdx = -1
        failOffset = -1

        if not curDiscard and not nodeDiscard and not nextEntDiscard and chars.find("/-", pos, endPos) == -1:
            # HOT LOOP A: Zero-slashdash chunk
            for k, pstr, pv, astr, arg, nh, fb in tokIter:
                if k:
                    if state != 1 or k in keywords:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    if not pv:
                        props[k] = pstr
                        continue
                    av = atomsGet(pv, unset)
                    if av is not unset:
                        props[k] = av
                        continue
                    vc = pv[0]
                    if "1" <= vc <= "9":
                        if "." not in pv and "e" not in pv and "E" not in pv:
                            try:
                                props[k] = intFn(pv, 10)
                            except ValueError:
                                props[k] = numDecFn(pv)
                        elif "." in pv:
                            try:
                                props[k] = floatFn(pv)
                            except ValueError:
                                props[k] = floatFn(pv.replace("_", ""))
                        else:
                            props[k] = numOtherFn(pv)
                    elif vc == '"':
                        try:
                            props[k] = unescFn(pv[1:-1])
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    else:
                        props[k] = numOtherFn(pv)
                    continue

                if nh:
                    if nh == "}":
                        if not stack:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                            stackPop()
                        )
                        if not pND:
                            parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                        curNodes = parentNodes
                        curAppend = parentAppend
                        state = 2
                        continue
                    lc = nh[-1]
                    if lc == '"':
                        sp = nh.find(" ")
                        nName = nh[:sp]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        name = nName
                        tag = None
                        args = [nh[sp + 2 : -1]]
                        argsAppend = args.append
                        props = {}
                        state = 1
                        continue
                    if lc == "{":
                        bName = nh[:-2]
                        if bName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                bName,
                                None,
                                [],
                                {},
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if nh[0] != "(":
                        if nh in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag: str | None = None
                        nName = nh
                    else:
                        rp = nh.find(")", 1)
                        nTag = nh[1:rp]
                        nName = nh[rp + 1 :]
                        if nTag in keywords or nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    if state == 1:
                        curAppend(nodeCls(name, tag, args, props, []))
                    name = nName
                    tag = nTag
                    args = []
                    argsAppend = args.append
                    props = {}
                    state = 1
                    continue

                if astr:
                    if state != 1:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    argsAppend(astr)
                    continue

                if arg:
                    ac0 = arg[0]
                    if state != 1:
                        if state == 0:
                            if ac0 == "}":
                                if not stack:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                                    stackPop()
                                )
                                if not pND:
                                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                                curNodes = parentNodes
                                curAppend = parentAppend
                                state = 2
                                continue
                            if (
                                ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                                and not ("0" <= ac0 <= "9")
                                and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                                and arg not in keywords
                            ):
                                name = arg
                                tag = None
                                args = []
                                argsAppend = args.append
                                props = {}
                                state = 1
                                continue
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break

                    if ac0 == "{":
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if "1" <= ac0 <= "9":
                        if "." not in arg and "e" not in arg and "E" not in arg:
                            try:
                                argsAppend(intFn(arg, 10))
                            except ValueError:
                                argsAppend(numDecFn(arg))
                        elif "." in arg:
                            try:
                                argsAppend(floatFn(arg))
                            except ValueError:
                                argsAppend(floatFn(arg.replace("_", "")))
                        else:
                            argsAppend(numOtherFn(arg))
                        continue
                    if ac0 == '"':
                        inner = arg[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        argsAppend(inner)
                        continue
                    if ac0 == "(":
                        if nativeTagged:
                            cv = tagGet(arg, unset)
                            if cv is not unset:
                                argsAppend(cv)
                                continue
                        rp = arg.find(")", 1)
                        tName = arg[1:rp]
                        if tName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        vStr = arg[rp + 1 :]
                        if vStr[0] == '"':
                            inner = vStr[1:-1]
                            if "\\" in inner:
                                try:
                                    inner = unescFn(inner)
                                except ValueError:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                            if nativeTagged:
                                conv = convStrFn(tName, inner)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                    tagCache[arg] = conv
                                argsAppend(conv)
                            else:
                                argsAppend(strCls(inner, tName))
                        elif nativeTagged:
                            conv = convDecFn(tName, vStr)
                            if conv is unset:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                tagCache[arg] = conv
                            argsAppend(conv)
                        else:
                            dObj = decodeRawNumFn(vStr)
                            dObj.tag = tName
                            argsAppend(dObj)
                        continue
                    if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                        # For `r###"body"###`, `q` is the opening `"` index (`q - 1` hashes on each side),
                        # so the closing `"` sits at `len(arg) - q`.
                        q = arg.find('"', 1)
                        argsAppend(arg[q + 1 : len(arg) - q])
                        continue
                    if ac0 == "/":
                        if "/*" in arg[2:-2]:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        continue
                    if ac0 == "\\":
                        continue
                    if ac0 == "}":
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    av = atomsGet(arg, unset)
                    if av is not unset:
                        argsAppend(av)
                        continue
                    if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                        argsAppend(numOtherFn(arg))
                        continue
                    failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if fb:
                    if fb == ";":
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                            state = 0
                            continue
                        if state == 2:
                            state = 0
                            continue
                    elif fb == "{" and state == 1:
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if lengthHint(tokIter) == 0:
                        failOffset = endPos - len(fb)
                    else:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if state == 1:
                    curAppend(nodeCls(name, tag, args, props, []))
                state = 0
        else:
            # HOT LOOP B: Slashdash-capable chunk
            for k, pstr, pv, astr, arg, nh, fb in tokIter:
                if k:
                    if state != 1 or k in keywords:
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break
                    if nodeDiscard or nextEntDiscard:
                        nextEntDiscard = False
                        if pv and pv[0] == '"':
                            try:
                                unescFn(pv[1:-1])
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        continue
                    if not pv:
                        props[k] = pstr
                        continue
                    av = atomsGet(pv, unset)
                    if av is not unset:
                        props[k] = av
                        continue
                    vc = pv[0]
                    if "1" <= vc <= "9":
                        if "." not in pv and "e" not in pv and "E" not in pv:
                            try:
                                props[k] = intFn(pv, 10)
                            except ValueError:
                                props[k] = numDecFn(pv)
                        elif "." in pv:
                            try:
                                props[k] = floatFn(pv)
                            except ValueError:
                                props[k] = floatFn(pv.replace("_", ""))
                        else:
                            props[k] = numOtherFn(pv)
                    elif vc == '"':
                        try:
                            props[k] = unescFn(pv[1:-1])
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    else:
                        props[k] = numOtherFn(pv)
                    continue

                if nh:
                    if nextEntDiscard:
                        failIdx = sdTokIdx
                        nextEntDiscard = False
                        break
                    if nh == "}":
                        if not stack:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                            stackPop()
                        )
                        if not pND:
                            parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                        curNodes = parentNodes
                        curAppend = parentAppend
                        state = 2
                        continue
                    lc = nh[-1]
                    if lc == '"':
                        sp = nh.find(" ")
                        nName = nh[:sp]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        name = nName
                        tag = None
                        args = [] if curDiscard else [nh[sp + 2 : -1]]
                        argsAppend = args.append
                        props = {}
                        nodeDiscard = curDiscard
                        state = 1
                        continue
                    if lc == "{":
                        bName = nh[:-2]
                        if bName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                bName,
                                None,
                                [],
                                {},
                                curDiscard,
                                curDiscard,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    hc0 = nh[0]
                    if hc0 not in ("/", "("):
                        if nh in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag = None
                        nName = nh
                        nDisc = curDiscard
                    elif hc0 == "/":
                        nName = nh[3:]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag = None
                        nDisc = True
                    else:
                        rp = nh.find(")", 1)
                        nTag = nh[1:rp]
                        nName = nh[rp + 1 :]
                        if nTag in keywords or nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nDisc = curDiscard
                    if state == 1 and not nodeDiscard:
                        curAppend(nodeCls(name, tag, args, props, []))
                    name = nName
                    tag = nTag
                    args = []
                    argsAppend = args.append
                    props = {}
                    nodeDiscard = nDisc
                    state = 1
                    continue

                if astr:
                    if state != 1:
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break
                    entDiscard = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    if not entDiscard:
                        argsAppend(astr)
                    continue

                if arg:
                    ac0 = arg[0]
                    if state != 1:
                        if state == 0:
                            if ac0 == "}":
                                if not stack:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                                    stackPop()
                                )
                                if not pND:
                                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                                curNodes = parentNodes
                                curAppend = parentAppend
                                state = 2
                                continue
                            if (
                                ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                                and not ("0" <= ac0 <= "9")
                                and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                                and arg not in keywords
                            ):
                                name = arg
                                tag = None
                                args = []
                                argsAppend = args.append
                                props = {}
                                nodeDiscard = curDiscard
                                state = 1
                                continue
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break

                    if ac0 == "{":
                        childDisc = nodeDiscard or nextEntDiscard
                        nextEntDiscard = False
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                nodeDiscard,
                                childDisc,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        curDiscard = childDisc
                        state = 0
                        continue
                    if ac0 == "/":
                        if arg[1] == "-":
                            if nextEntDiscard:
                                failIdx = sdTokIdx
                                nextEntDiscard = False
                                break
                            nextEntDiscard = True
                            sdTokIdx = nToks - 1 - lengthHint(tokIter)
                            continue
                        if "/*" in arg[2:-2]:
                            failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                            nextEntDiscard = False
                            break
                        continue
                    if ac0 == "\\":
                        continue
                    if ac0 == "}":
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break

                    entDiscard = nodeDiscard or nextEntDiscard
                    if ac0 == '"':
                        nextEntDiscard = False
                        inner = arg[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        if not entDiscard:
                            argsAppend(inner)
                        continue
                    if ac0 == "(":
                        rp = arg.find(")", 1)
                        tName = arg[1:rp]
                        if tName in keywords:
                            failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                            nextEntDiscard = False
                            break
                        vStr = arg[rp + 1 :]
                        if vStr[0] == '"':
                            inner = vStr[1:-1]
                            if "\\" in inner:
                                try:
                                    inner = unescFn(inner)
                                except ValueError:
                                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                                    nextEntDiscard = False
                                    break
                            nextEntDiscard = False
                            if not entDiscard:
                                if nativeTagged:
                                    cv = tagGet(arg, unset)
                                    if cv is not unset:
                                        argsAppend(cv)
                                        continue
                                    conv = convStrFn(tName, inner)
                                    if conv is unset:
                                        failIdx = nToks - 1 - lengthHint(tokIter)
                                        break
                                    if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                        tagCache[arg] = conv
                                    argsAppend(conv)
                                else:
                                    argsAppend(strCls(inner, tName))
                            continue
                        nextEntDiscard = False
                        if not entDiscard:
                            if nativeTagged:
                                cv = tagGet(arg, unset)
                                if cv is not unset:
                                    argsAppend(cv)
                                    continue
                                conv = convDecFn(tName, vStr)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                    tagCache[arg] = conv
                                argsAppend(conv)
                            else:
                                dObj = decodeRawNumFn(vStr)
                                dObj.tag = tName
                                argsAppend(dObj)
                        continue
                    if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                        nextEntDiscard = False
                        if not entDiscard:
                            q = arg.find('"', 1)
                            argsAppend(arg[q + 1 : len(arg) - q])
                        continue
                    if "1" <= ac0 <= "9":
                        nextEntDiscard = False
                        if not entDiscard:
                            if "." not in arg and "e" not in arg and "E" not in arg:
                                try:
                                    argsAppend(intFn(arg, 10))
                                except ValueError:
                                    argsAppend(numDecFn(arg))
                            elif "." in arg:
                                try:
                                    argsAppend(floatFn(arg))
                                except ValueError:
                                    argsAppend(floatFn(arg.replace("_", "")))
                            else:
                                argsAppend(numOtherFn(arg))
                        continue
                    av = atomsGet(arg, unset)
                    if av is not unset:
                        nextEntDiscard = False
                        if not entDiscard:
                            argsAppend(av)
                        continue
                    if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                        nextEntDiscard = False
                        if not entDiscard:
                            argsAppend(numOtherFn(arg))
                        continue
                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                    nextEntDiscard = False
                    break

                if fb:
                    if fb == ";":
                        if nextEntDiscard:
                            failIdx = sdTokIdx
                            nextEntDiscard = False
                            break
                        if state == 1:
                            if not nodeDiscard:
                                curAppend(nodeCls(name, tag, args, props, []))
                            state = 0
                            continue
                        if state == 2:
                            state = 0
                            continue
                    elif fb == "{" and state == 1:
                        childDisc = nodeDiscard or nextEntDiscard
                        nextEntDiscard = False
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                nodeDiscard,
                                childDisc,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        curDiscard = childDisc
                        state = 0
                        continue
                    if nextEntDiscard:
                        failIdx = sdTokIdx
                        nextEntDiscard = False
                    elif lengthHint(tokIter) == 0:
                        failOffset = endPos - len(fb)
                    else:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if nextEntDiscard:
                    failIdx = sdTokIdx
                    nextEntDiscard = False
                    break
                if state == 1 and not nodeDiscard:
                    curAppend(nodeCls(name, tag, args, props, []))
                state = 0

        if failIdx < 0 and failOffset < 0:
            if nextEntDiscard:
                failIdx = sdTokIdx
                nextEntDiscard = False
            else:
                pos = endPos
                chunkSize = min(chunkSize * 4, 32768)
                continue

        i = failOffset if failOffset >= 0 else _tokenOffset(chars, pos, endPos, failIdx)
        chunkSize = max(256, chunkSize // 4)
        if s is None:
            s = Stream(chars, config)
        (
            newPos,
            state,
            name,
            tag,
            args,
            props,
            nodeDiscard,
            curNodes,
            curDiscard,
        ) = _handleChunkFallback(
            s,
            i,
            nativeUntagged=True,
            nativeTagged=nativeTagged,
            state=state,
            name=name,
            tag=tag,
            args=args,
            props=props,
            nodeDiscard=nodeDiscard,
            curNodes=curNodes,
            curDiscard=curDiscard,
            stack=stack,
        )
        if newPos < 0:
            return Document(curNodes)
        curAppend = curNodes.append
        argsAppend = args.append
        pos = newPos

    if state == 1 and not nodeDiscard:
        curAppend(nodeCls(name, tag, args, props, []))
    if stack:
        if s is None:
            s = Stream(chars, config)
        raise ParseError(s, _unclosedBlockPos(chars, stack[-1]), "Hit EOF while searching for end of child list")
    return Document(curNodes)


def _parseChunkedRaw(chars: str, config: t.ParseConfig) -> Document:
    """Specialized chunked C-regex parser for nativeUntaggedValues=False with no custom converters."""
    n = len(chars)
    nativeTagged = config.nativeTaggedValues
    findall = _tokFindall
    lengthHint = _lengthHint
    keywords = _KEYWORDS
    unset = _UNSET
    nodeCls = Node
    strCls = String
    rawStrCls = RawString
    boolCls = Bool
    nullCls = Null
    decCls = Decimal
    valueCls = Value
    intFn = int
    floatFn = float
    unescFn = _unescapeValidated
    decodeRawNumFn = _decodeRawNum
    convStrFn = _convertTaggedStringFast
    convDecFn = _convertTaggedDecFast

    s: Stream | None = None
    curNodes: list[Node] = []
    curAppend = curNodes.append
    curDiscard = False
    stack: list[_FastFrame] = []
    stackPush = stack.append
    stackPop = stack.pop
    lineCache: dict[str, t.Any] = {}
    tagCache: dict[str, t.Any] = {}
    tagGet = tagCache.get
    validIds: set[str] = set()

    state = 0
    name = ""
    tag: str | None = None
    args: list[t.Any] = []
    argsAppend = args.append
    props: dict[str, t.Any] = {}
    nodeDiscard = False
    nextEntDiscard = False
    sdTokIdx = 0

    pos = 0
    chunkSize = 4096 if n >= 2048 else 512
    while pos < n:
        endPos = _pickChunkEnd(chars, pos, n, chunkSize)
        if endPos < 0:
            if s is None:
                s = Stream(chars, config)
            (
                newPos,
                state,
                name,
                tag,
                args,
                props,
                nodeDiscard,
                curNodes,
                curDiscard,
            ) = _handleChunkFallback(
                s,
                pos,
                nativeUntagged=False,
                nativeTagged=nativeTagged,
                state=state,
                name=name,
                tag=tag,
                args=args,
                props=props,
                nodeDiscard=nodeDiscard,
                curNodes=curNodes,
                curDiscard=curDiscard,
                stack=stack,
            )
            if newPos < 0:
                return Document(curNodes)
            curAppend = curNodes.append
            argsAppend = args.append
            pos = newPos
            continue

        # Tier 2 (RAW): Adaptive line-template recipe cache for repeating lines (when nativeTagged=False).
        if (
            not nativeTagged
            and state == 0
            and not curDiscard
            and not nodeDiscard
            and not nextEntDiscard
            and endPos - pos >= 512
        ):
            lcEnd = endPos
            consumeNl = lcEnd < n and chars[lcEnd] == "\n"
            if not consumeNl and lcEnd < n:
                lastNl = chars.rfind("\n", pos, lcEnd)
                if lastNl > pos:
                    lcEnd = lastNl
                    consumeNl = True
            if lcEnd - pos >= 512:
                chunkSlice = chars[pos:lcEnd]
                if (
                    "\r" not in chunkSlice
                    and "/*" not in chunkSlice
                    and "\x0c" not in chunkSlice
                    and (
                        chunkSlice.isascii()
                        or ("\x85" not in chunkSlice and "\u2028" not in chunkSlice and "\u2029" not in chunkSlice)
                    )
                ):
                    chunkLines = chunkSlice.split("\n")
                    if lineCache or _hasRepeatingLines(chunkLines):
                        lcPos, curNodes = _tryLineCachedChunkRaw(
                            chunkLines,
                            pos,
                            lcEnd,
                            consumeTrailingNl=consumeNl,
                            curNodes=curNodes,
                            stack=stack,
                            lineCache=lineCache,
                            validIds=validIds,
                        )
                        curAppend = curNodes.append
                        targetPos = lcEnd + 1 if consumeNl else lcEnd
                        if lcPos >= targetPos:
                            pos = lcPos
                            chunkSize = min(chunkSize * 4, 65536)
                            continue
                        if s is None:
                            s = Stream(chars, config)
                        (
                            newPos,
                            state,
                            name,
                            tag,
                            args,
                            props,
                            nodeDiscard,
                            curNodes,
                            curDiscard,
                        ) = _handleChunkFallback(
                            s,
                            lcPos,
                            nativeUntagged=False,
                            nativeTagged=False,
                            state=0,
                            name="",
                            tag=None,
                            args=[],
                            props={},
                            nodeDiscard=False,
                            curNodes=curNodes,
                            curDiscard=False,
                            stack=stack,
                        )
                        if newPos < 0:
                            return Document(curNodes)
                        curAppend = curNodes.append
                        argsAppend = args.append
                        pos = newPos
                        chunkSize = max(512, chunkSize // 2)
                        continue

        if pos == 0 and state == 0:
            mh0 = _matchNodeHead(chars, 0)
            assert mh0 is not None
            li0 = mh0.lastindex
            if li0 == 1:
                cand0 = mh0.group(1)
                if cand0 not in keywords:
                    name = cand0
                    pos = mh0.end()
                    state = 1
                    if pos >= endPos:
                        continue
            elif li0 is None and mh0.end() >= n:
                return Document(curNodes)

        # Parser state: 0 = expecting node head or `}`, 1 = inside node (entries / `{` / terminator),
        # 2 = immediately after `}` (expecting node terminator).
        # `nToks - 1 - lengthHint(tokIter)` recovers the current token index on rare fallback without `enumerate()`.
        toks = findall(chars, pos, endPos)
        nToks = len(toks)
        tokIter = iter(toks)
        failIdx = -1
        failOffset = -1

        if not curDiscard and not nodeDiscard and not nextEntDiscard and chars.find("/-", pos, endPos) == -1:
            # HOT LOOP A (RAW): Zero-slashdash chunk
            for k, pstr, pv, astr, arg, nh, fb in tokIter:
                if k:
                    if state != 1 or k in keywords:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    if not pv:
                        props[k] = strCls(pstr)
                        continue
                    vc = pv[0]
                    if "1" <= vc <= "9":
                        if "." not in pv and "e" not in pv and "E" not in pv:
                            try:
                                props[k] = decCls(intFn(pv, 10), 0)
                            except ValueError:
                                props[k] = decodeRawNumFn(pv)
                        elif "e" not in pv and "E" not in pv:
                            try:
                                props[k] = decCls(floatFn(pv), 0)
                            except ValueError:
                                props[k] = decCls(floatFn(pv.replace("_", "")), 0)
                        else:
                            props[k] = decodeRawNumFn(pv)
                    elif vc in ("t", "f", "n"):
                        props[k] = boolCls(True) if pv == "true" else (boolCls(False) if pv == "false" else nullCls())
                    elif vc == '"':
                        try:
                            props[k] = strCls(unescFn(pv[1:-1]))
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    else:
                        props[k] = decodeRawNumFn(pv)
                    continue

                if nh:
                    if nh == "}":
                        if not stack:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                            stackPop()
                        )
                        if not pND:
                            parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                        curNodes = parentNodes
                        curAppend = parentAppend
                        state = 2
                        continue
                    lc = nh[-1]
                    if lc == '"':
                        sp = nh.find(" ")
                        nName = nh[:sp]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        name = nName
                        tag = None
                        args = [strCls(nh[sp + 2 : -1])]
                        argsAppend = args.append
                        props = {}
                        state = 1
                        continue
                    if lc == "{":
                        bName = nh[:-2]
                        if bName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                bName,
                                None,
                                [],
                                {},
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if nh[0] != "(":
                        if nh in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag: str | None = None
                        nName = nh
                    else:
                        rp = nh.find(")", 1)
                        nTag = nh[1:rp]
                        nName = nh[rp + 1 :]
                        if nTag in keywords or nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    if state == 1:
                        curAppend(nodeCls(name, tag, args, props, []))
                    name = nName
                    tag = nTag
                    args = []
                    argsAppend = args.append
                    props = {}
                    state = 1
                    continue

                if astr:
                    if state != 1:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    argsAppend(strCls(astr))
                    continue

                if arg:
                    ac0 = arg[0]
                    if state != 1:
                        if state == 0:
                            if ac0 == "}":
                                if not stack:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                                    stackPop()
                                )
                                if not pND:
                                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                                curNodes = parentNodes
                                curAppend = parentAppend
                                state = 2
                                continue
                            if (
                                ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                                and not ("0" <= ac0 <= "9")
                                and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                                and arg not in keywords
                            ):
                                name = arg
                                tag = None
                                args = []
                                argsAppend = args.append
                                props = {}
                                state = 1
                                continue
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break

                    if ac0 == "{":
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if "1" <= ac0 <= "9":
                        if "." not in arg and "e" not in arg and "E" not in arg:
                            try:
                                argsAppend(decCls(intFn(arg, 10), 0))
                            except ValueError:
                                argsAppend(decodeRawNumFn(arg))
                        elif "e" not in arg and "E" not in arg:
                            try:
                                argsAppend(decCls(floatFn(arg), 0))
                            except ValueError:
                                argsAppend(decCls(floatFn(arg.replace("_", "")), 0))
                        else:
                            argsAppend(decodeRawNumFn(arg))
                        continue
                    if ac0 == '"':
                        inner = arg[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        argsAppend(strCls(inner))
                        continue
                    if ac0 == "(":
                        if nativeTagged:
                            cv = tagGet(arg, unset)
                            if cv is not unset:
                                argsAppend(cv)
                                continue
                        rp = arg.find(")", 1)
                        tName = arg[1:rp]
                        if tName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        vStr = arg[rp + 1 :]
                        if vStr[0] == '"':
                            inner = vStr[1:-1]
                            if "\\" in inner:
                                try:
                                    inner = unescFn(inner)
                                except ValueError:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                            if nativeTagged:
                                conv = convStrFn(tName, inner)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                    tagCache[arg] = conv
                                argsAppend(conv)
                            else:
                                argsAppend(strCls(inner, tName))
                        elif nativeTagged:
                            conv = convDecFn(tName, vStr)
                            if conv is unset:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                tagCache[arg] = conv
                            argsAppend(conv)
                        else:
                            dObj = decodeRawNumFn(vStr)
                            dObj.tag = tName
                            argsAppend(dObj)
                        continue
                    if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                        # For `r###"body"###`, `q` is the opening `"` index (`q - 1` hashes on each side),
                        # so the closing `"` sits at `len(arg) - q`.
                        q = arg.find('"', 1)
                        argsAppend(rawStrCls(arg[q + 1 : len(arg) - q]))
                        continue
                    if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                        argsAppend(decodeRawNumFn(arg))
                        continue
                    if arg in keywords:
                        argsAppend(
                            boolCls(True) if arg == "true" else (boolCls(False) if arg == "false" else nullCls()),
                        )
                        continue
                    if ac0 == "/":
                        if "/*" in arg[2:-2]:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        continue
                    if ac0 == "\\":
                        continue
                    failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if fb:
                    if fb == ";":
                        if state == 1:
                            curAppend(nodeCls(name, tag, args, props, []))
                            state = 0
                            continue
                        if state == 2:
                            state = 0
                            continue
                    elif fb == "{" and state == 1:
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                False,
                                False,
                                False,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    if lengthHint(tokIter) == 0:
                        failOffset = endPos - len(fb)
                    else:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if state == 1:
                    curAppend(nodeCls(name, tag, args, props, []))
                state = 0
        else:
            # HOT LOOP B (RAW): Slashdash-capable chunk
            for k, pstr, pv, astr, arg, nh, fb in tokIter:
                if k:
                    if state != 1 or k in keywords:
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break
                    if nodeDiscard or nextEntDiscard:
                        nextEntDiscard = False
                        if pv and pv[0] == '"':
                            try:
                                unescFn(pv[1:-1])
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        continue
                    if not pv:
                        props[k] = strCls(pstr)
                        continue
                    vc = pv[0]
                    if "1" <= vc <= "9":
                        if "." not in pv and "e" not in pv and "E" not in pv:
                            try:
                                props[k] = decCls(intFn(pv, 10), 0)
                            except ValueError:
                                props[k] = decodeRawNumFn(pv)
                        elif "e" not in pv and "E" not in pv:
                            try:
                                props[k] = decCls(floatFn(pv), 0)
                            except ValueError:
                                props[k] = decCls(floatFn(pv.replace("_", "")), 0)
                        else:
                            props[k] = decodeRawNumFn(pv)
                    elif vc in ("t", "f", "n"):
                        props[k] = boolCls(True) if pv == "true" else (boolCls(False) if pv == "false" else nullCls())
                    elif vc == '"':
                        try:
                            props[k] = strCls(unescFn(pv[1:-1]))
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    else:
                        props[k] = decodeRawNumFn(pv)
                    continue

                if nh:
                    if nextEntDiscard:
                        failIdx = sdTokIdx
                        nextEntDiscard = False
                        break
                    if nh == "}":
                        if not stack:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                            stackPop()
                        )
                        if not pND:
                            parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                        curNodes = parentNodes
                        curAppend = parentAppend
                        state = 2
                        continue
                    lc = nh[-1]
                    if lc == '"':
                        sp = nh.find(" ")
                        nName = nh[:sp]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        name = nName
                        tag = None
                        args = [] if curDiscard else [strCls(nh[sp + 2 : -1])]
                        argsAppend = args.append
                        props = {}
                        nodeDiscard = curDiscard
                        state = 1
                        continue
                    if lc == "{":
                        bName = nh[:-2]
                        if bName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        if state == 1 and not nodeDiscard:
                            curAppend(nodeCls(name, tag, args, props, []))
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                bName,
                                None,
                                [],
                                {},
                                curDiscard,
                                curDiscard,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        state = 0
                        continue
                    hc0 = nh[0]
                    if hc0 not in ("/", "("):
                        if nh in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag = None
                        nName = nh
                        nDisc = curDiscard
                    elif hc0 == "/":
                        nName = nh[3:]
                        if nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nTag = None
                        nDisc = True
                    else:
                        rp = nh.find(")", 1)
                        nTag = nh[1:rp]
                        nName = nh[rp + 1 :]
                        if nTag in keywords or nName in keywords:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                        nDisc = curDiscard
                    if state == 1 and not nodeDiscard:
                        curAppend(nodeCls(name, tag, args, props, []))
                    name = nName
                    tag = nTag
                    args = []
                    argsAppend = args.append
                    props = {}
                    nodeDiscard = nDisc
                    state = 1
                    continue

                if astr:
                    if state != 1:
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break
                    entDiscard = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    if not entDiscard:
                        argsAppend(strCls(astr))
                    continue

                if arg:
                    ac0 = arg[0]
                    if state != 1:
                        if state == 0:
                            if ac0 == "}":
                                if not stack:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                parentNodes, parentAppend, pName, pTag, pArgs, pProps, pND, pCD, curDiscard, _, _, _ = (
                                    stackPop()
                                )
                                if not pND:
                                    parentAppend(nodeCls(pName, pTag, pArgs, pProps, [] if pCD else curNodes))
                                curNodes = parentNodes
                                curAppend = parentAppend
                                state = 2
                                continue
                            if (
                                ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                                and not ("0" <= ac0 <= "9")
                                and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                                and arg not in keywords
                            ):
                                name = arg
                                tag = None
                                args = []
                                argsAppend = args.append
                                props = {}
                                nodeDiscard = curDiscard
                                state = 1
                                continue
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break

                    if ac0 == "{":
                        childDisc = nodeDiscard or nextEntDiscard
                        nextEntDiscard = False
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                nodeDiscard,
                                childDisc,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        curDiscard = childDisc
                        state = 0
                        continue
                    if ac0 == "/":
                        if arg[1] == "-":
                            if nextEntDiscard:
                                failIdx = sdTokIdx
                                nextEntDiscard = False
                                break
                            nextEntDiscard = True
                            sdTokIdx = nToks - 1 - lengthHint(tokIter)
                            continue
                        if "/*" in arg[2:-2]:
                            failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                            nextEntDiscard = False
                            break
                        continue
                    if ac0 == "\\":
                        continue
                    if ac0 == "}":
                        failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                        nextEntDiscard = False
                        break

                    entDiscard = nodeDiscard or nextEntDiscard
                    if ac0 == '"':
                        nextEntDiscard = False
                        inner = arg[1:-1]
                        if "\\" in inner:
                            try:
                                inner = unescFn(inner)
                            except ValueError:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                        if not entDiscard:
                            argsAppend(strCls(inner))
                        continue
                    if ac0 == "(":
                        rp = arg.find(")", 1)
                        tName = arg[1:rp]
                        if tName in keywords:
                            failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                            nextEntDiscard = False
                            break
                        vStr = arg[rp + 1 :]
                        if vStr[0] == '"':
                            inner = vStr[1:-1]
                            if "\\" in inner:
                                try:
                                    inner = unescFn(inner)
                                except ValueError:
                                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                                    nextEntDiscard = False
                                    break
                            nextEntDiscard = False
                            if not entDiscard:
                                if nativeTagged:
                                    cv = tagGet(arg, unset)
                                    if cv is not unset:
                                        argsAppend(cv)
                                        continue
                                    conv = convStrFn(tName, inner)
                                    if conv is unset:
                                        failIdx = nToks - 1 - lengthHint(tokIter)
                                        break
                                    if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                        tagCache[arg] = conv
                                    argsAppend(conv)
                                else:
                                    argsAppend(strCls(inner, tName))
                            continue
                        nextEntDiscard = False
                        if not entDiscard:
                            if nativeTagged:
                                cv = tagGet(arg, unset)
                                if cv is not unset:
                                    argsAppend(cv)
                                    continue
                                conv = convDecFn(tName, vStr)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                if not isinstance(conv, valueCls) and len(tagCache) < 1024:
                                    tagCache[arg] = conv
                                argsAppend(conv)
                            else:
                                dObj = decodeRawNumFn(vStr)
                                dObj.tag = tName
                                argsAppend(dObj)
                        continue
                    if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                        nextEntDiscard = False
                        if not entDiscard:
                            q = arg.find('"', 1)
                            argsAppend(rawStrCls(arg[q + 1 : len(arg) - q]))
                        continue
                    if "1" <= ac0 <= "9":
                        nextEntDiscard = False
                        if not entDiscard:
                            if "." not in arg and "e" not in arg and "E" not in arg:
                                try:
                                    argsAppend(decCls(intFn(arg, 10), 0))
                                except ValueError:
                                    argsAppend(decodeRawNumFn(arg))
                            elif "e" not in arg and "E" not in arg:
                                try:
                                    argsAppend(decCls(floatFn(arg), 0))
                                except ValueError:
                                    argsAppend(decCls(floatFn(arg.replace("_", "")), 0))
                            else:
                                argsAppend(decodeRawNumFn(arg))
                        continue
                    if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and "0" <= arg[1] <= "9"):
                        nextEntDiscard = False
                        if not entDiscard:
                            argsAppend(decodeRawNumFn(arg))
                        continue
                    if arg in keywords:
                        nextEntDiscard = False
                        if not entDiscard:
                            argsAppend(
                                boolCls(True) if arg == "true" else (boolCls(False) if arg == "false" else nullCls()),
                            )
                        continue
                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                    nextEntDiscard = False
                    break

                if fb:
                    if fb == ";":
                        if nextEntDiscard:
                            failIdx = sdTokIdx
                            nextEntDiscard = False
                            break
                        if state == 1:
                            if not nodeDiscard:
                                curAppend(nodeCls(name, tag, args, props, []))
                            state = 0
                            continue
                        if state == 2:
                            state = 0
                            continue
                    elif fb == "{" and state == 1:
                        childDisc = nodeDiscard or nextEntDiscard
                        nextEntDiscard = False
                        stackPush(
                            (
                                curNodes,
                                curAppend,
                                name,
                                tag,
                                args,
                                props,
                                nodeDiscard,
                                childDisc,
                                curDiscard,
                                pos,
                                endPos,
                                nToks - 1 - lengthHint(tokIter),
                            ),
                        )
                        curNodes = []
                        curAppend = curNodes.append
                        curDiscard = childDisc
                        state = 0
                        continue
                    if nextEntDiscard:
                        failIdx = sdTokIdx
                        nextEntDiscard = False
                    elif lengthHint(tokIter) == 0:
                        failOffset = endPos - len(fb)
                    else:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if nextEntDiscard:
                    failIdx = sdTokIdx
                    nextEntDiscard = False
                    break
                if state == 1 and not nodeDiscard:
                    curAppend(nodeCls(name, tag, args, props, []))
                state = 0

        if failIdx < 0 and failOffset < 0:
            if nextEntDiscard:
                failIdx = sdTokIdx
                nextEntDiscard = False
            else:
                pos = endPos
                chunkSize = min(chunkSize * 4, 32768)
                continue

        i = failOffset if failOffset >= 0 else _tokenOffset(chars, pos, endPos, failIdx)
        chunkSize = max(256, chunkSize // 4)
        if s is None:
            s = Stream(chars, config)
        (
            newPos,
            state,
            name,
            tag,
            args,
            props,
            nodeDiscard,
            curNodes,
            curDiscard,
        ) = _handleChunkFallback(
            s,
            i,
            nativeUntagged=False,
            nativeTagged=nativeTagged,
            state=state,
            name=name,
            tag=tag,
            args=args,
            props=props,
            nodeDiscard=nodeDiscard,
            curNodes=curNodes,
            curDiscard=curDiscard,
            stack=stack,
        )
        if newPos < 0:
            return Document(curNodes)
        curAppend = curNodes.append
        argsAppend = args.append
        pos = newPos

    if state == 1 and not nodeDiscard:
        curAppend(nodeCls(name, tag, args, props, []))
    if stack:
        if s is None:
            s = Stream(chars, config)
        raise ParseError(s, _unclosedBlockPos(chars, stack[-1]), "Hit EOF while searching for end of child list")
    return Document(curNodes)


def _applyNodeConverters(s: Stream, node: Node, nodeStart: int, nameEnd: int) -> Node:
    """Apply the first matching user `nodeConverter` (that does not return `NotImplemented`) to `node`."""
    pf: ParseFragment | None = None
    for convKey, converter in s.config.nodeConverters.items():
        if node.matchesKey(convKey):
            if pf is None:
                pf = ParseFragment(s.chars[nodeStart:nameEnd], s, nodeStart)
            converted = converter(node, pf)
            if converted == NotImplemented:
                continue
            node = converted
            break
    return node


def _parseDirect(chars: str, config: t.ParseConfig) -> Document:
    """Monolithic iterative parser for tiny inputs and configs with custom converters (lazy Stream allocation)."""
    n = len(chars)
    hasNodeConverters = bool(config.nodeConverters)
    hasValConverters = bool(config.valueConverters)
    fastUntagged = config.nativeUntaggedValues and not hasValConverters
    matchHead = _matchNodeHead
    matchEntry = _matchEntry
    kwMap = _KW_NATIVE
    atomsGet = _ATOMS.get
    unset = _UNSET
    keywords = _KEYWORDS
    wsSet = _WS_SET
    nlSet = _NL_SET
    digits = _DIGITS
    nodeCls = Node
    strCls = String
    boolCls = Bool
    nullCls = Null
    decCls = Decimal
    intFn = int

    s: Stream | None = None
    curNodes: list[Node] = []
    curDiscard = False
    stack: list[_GenFrame] = []
    i = 0

    while True:
        mh = matchHead(chars, i)
        assert mh is not None
        li = mh.lastindex
        tag: str | None = None
        nodeDiscard = curDiscard
        if li == 1:
            nodeStart = mh.start(1)
            name = mh.group(1)
            if name in keywords:
                if s is None:
                    s = Stream(chars, config)
                raise ParseError(
                    s,
                    nodeStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            i = mh.end()
            nameEnd = i
        elif li == 3:
            if not stack:
                if s is None:
                    s = Stream(chars, config)
                raise ParseError(s, mh.start(3), "Expected a node")
            i = mh.end()
            (
                parentNodes,
                pStart,
                pNameEnd,
                pName,
                pTag,
                pArgs,
                pProps,
                pNodeDiscard,
                pChildDiscard,
                pParentDiscard,
                _,
            ) = stack.pop()
            if i < n:
                chEnd = chars[i]
                if chEnd in ("\n", ";"):
                    i += 1
                else:
                    if s is None:
                        s = Stream(chars, config)
                    if chEnd in wsSet or chEnd == "\\" or (chEnd == "/" and i + 1 < n and chars[i + 1] == "*"):
                        i = _skipNodespace(s, i)
                    i = _skipTerminator(s, i)
            if not pNodeDiscard:
                built = nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes)
                if hasNodeConverters:
                    if s is None:
                        s = Stream(chars, config)
                    built = _applyNodeConverters(s, built, pStart, pNameEnd)
                parentNodes.append(built)
            curNodes = parentNodes
            curDiscard = pParentDiscard
            continue
        elif li is None:
            i = mh.end()
            if i >= n:
                if stack:
                    if s is None:
                        s = Stream(chars, config)
                    raise ParseError(s, stack[-1][10], "Hit EOF while searching for end of child list")
                return Document(curNodes)
            if s is None:
                s = Stream(chars, config)
            raise ParseError(
                s,
                i,
                "Junk between end of child list and closing }" if stack else "Expected a node",
            )
        elif li == 2:
            nodeStart = mh.start(2) - 1
            name = mh.group(2)
            i = mh.end()
            nameEnd = i
        elif li == 5:
            nodeStart = mh.start(5)
            if s is None:
                s = Stream(chars, config)
            tag, tagEnd = _parseTag(s, nodeStart)
            if tagEnd < 0:
                raise ParseError(
                    s,
                    nodeStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            nameOrNone, nameEnd = _parseIdent(s, tagEnd)
            if nameOrNone is None:
                raise ParseError(
                    s,
                    nodeStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            name = nameOrNone
            i = nameEnd
        elif li == 4:
            nodeStart = mh.start(4)
            if s is None:
                s = Stream(chars, config)
            j = _skipNodespace(s, mh.end())
            if j < n and chars[j] == "(":
                tag, tagEnd = _parseTag(s, j)
                if tagEnd >= 0:
                    j = tagEnd
            nameOrNone, nameEnd = _parseIdent(s, j)
            if nameOrNone is None:
                raise ParseError(
                    s,
                    nodeStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            name = nameOrNone
            nodeDiscard = True
            i = nameEnd
        elif li == 6:
            if s is None:
                s = Stream(chars, config)
            i = _skipBlockComment(s, mh.start(6))
            continue
        elif li == 7:
            nodeStart = mh.start(7)
            if s is None:
                s = Stream(chars, config)
            name, i = _parseEscapedString(s, nodeStart)
            nameEnd = i
        else:
            nodeStart = mh.start(8)
            if s is None:
                s = Stream(chars, config)
            name, i = _parseRawString(s, nodeStart, mh.end() - nodeStart - 2)
            nameEnd = i

        if i >= n:
            if not nodeDiscard:
                built = nodeCls(name, tag, [], {}, [])
                if hasNodeConverters:
                    if s is None:
                        s = Stream(chars, config)
                    built = _applyNodeConverters(s, built, nodeStart, nameEnd)
                curNodes.append(built)
            continue
        ch0 = chars[i]
        if ch0 in ("\n", ";"):
            i += 1
            if not nodeDiscard:
                built = nodeCls(name, tag, [], {}, [])
                if hasNodeConverters:
                    if s is None:
                        s = Stream(chars, config)
                    built = _applyNodeConverters(s, built, nodeStart, nameEnd)
                curNodes.append(built)
            continue
        if ch0 == "{":
            stack.append((curNodes, nodeStart, nameEnd, name, tag, [], {}, nodeDiscard, nodeDiscard, curDiscard, i))
            curNodes = []
            curDiscard = nodeDiscard
            i += 1
            continue

        args: list[t.Any] = []
        props: dict[str, t.Any] = {}
        enteredChildren = False

        while i < n:
            ch = chars[i]
            if ch == " ":
                i += 1
                if i >= n:
                    break
            elif ch in ("\n", ";"):
                i += 1
                break
            elif ch == "{":
                stack.append(
                    (curNodes, nodeStart, nameEnd, name, tag, args, props, nodeDiscard, nodeDiscard, curDiscard, i),
                )
                curNodes = []
                curDiscard = nodeDiscard
                i += 1
                enteredChildren = True
                break
            elif ch in wsSet or ch == "\\" or (ch == "/" and i + 1 < n and chars[i + 1] == "*"):
                if s is None:
                    s = Stream(chars, config)
                nextI = _skipNodespace(s, i)
                if nextI == i:
                    i = _skipTerminator(s, i)
                    break
                i = nextI
                if i >= n:
                    break
            elif ch == "/" and i + 1 < n and chars[i + 1] == "-":
                if s is None:
                    s = Stream(chars, config)
                j = _skipNodespace(s, i + 2)
                if j < n and chars[j] == "{":
                    stack.append(
                        (curNodes, nodeStart, nameEnd, name, tag, args, props, nodeDiscard, True, curDiscard, i),
                    )
                    curNodes = []
                    curDiscard = True
                    i = j + 1
                    enteredChildren = True
                    break
                raise ParseError(s, i, "Junk after node, before terminator.")
            else:
                if s is None:
                    s = Stream(chars, config)
                i = _skipTerminator(s, i)
                break

            while True:
                ch2 = chars[i]
                if ch2 == "{":
                    stack.append(
                        (curNodes, nodeStart, nameEnd, name, tag, args, props, nodeDiscard, nodeDiscard, curDiscard, i),
                    )
                    curNodes = []
                    curDiscard = nodeDiscard
                    i += 1
                    enteredChildren = True
                    break
                if ch2 in ("\n", ";"):
                    i += 1
                    ch2 = "\n"
                    break
                if ch2 == "(":
                    if s is None:
                        s = Stream(chars, config)
                    val, valEnd = _parseValue(s, i, fastUntagged, nodeDiscard)
                    if valEnd < 0:
                        raise ParseError(s, i, "Junk after node, before terminator.")
                    i = valEnd
                    if not nodeDiscard:
                        args.append(val)
                    break
                if ch2 in wsSet or ch2 == "\\" or (ch2 == "/" and i + 1 < n and chars[i + 1] == "*"):
                    if s is None:
                        s = Stream(chars, config)
                    nextI = _skipNodespace(s, i)
                    if nextI > i:
                        i = nextI
                        if i >= n:
                            break
                        continue
                    i = _skipTerminator(s, i)
                    ch2 = "\n"
                    break
                if ch2 == "/" and i + 1 < n:
                    chNext = chars[i + 1]
                    if chNext == "-":
                        sdStart = i
                        if s is None:
                            s = Stream(chars, config)
                        j = _skipNodespace(s, i + 2)
                        if j < n and chars[j] == "{":
                            stack.append(
                                (
                                    curNodes,
                                    nodeStart,
                                    nameEnd,
                                    name,
                                    tag,
                                    args,
                                    props,
                                    nodeDiscard,
                                    True,
                                    curDiscard,
                                    sdStart,
                                ),
                            )
                            curNodes = []
                            curDiscard = True
                            i = j + 1
                            enteredChildren = True
                            break
                        entEnd = _parseDiscardedEntity(s, j)
                        if entEnd < 0:
                            raise ParseError(s, sdStart, "Junk after node, before terminator.")
                        i = entEnd
                        break
                    if chNext == "/":
                        mc = _matchSingleLineComment(chars, i)
                        assert mc is not None
                        i = mc.end()
                        ch2 = "\n"
                        break

                m = matchEntry(chars, i)
                if m is not None:
                    eli = m.lastindex
                    if eli == 3:
                        i = m.end()
                        if not nodeDiscard:
                            key = m.group(1)
                            numStr = m.group(3)
                            if fastUntagged:
                                av = atomsGet(numStr, unset)
                                val = av if av is not unset else _numFromDecStr(numStr)
                            else:
                                if "." not in numStr and "e" not in numStr and "E" not in numStr:
                                    try:
                                        val = decCls(intFn(numStr, 10), 0)
                                    except ValueError:
                                        val = _decimalFromStr(numStr)
                                else:
                                    val = _decimalFromStr(numStr)
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, m.start(3), i)
                            props[key] = val
                        break
                    if eli == 2:
                        i = m.end()
                        if not nodeDiscard:
                            key = m.group(1)
                            sVal = m.group(2)
                            if fastUntagged:
                                val = sVal
                            else:
                                val = strCls(sVal)
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, m.start(2) - 1, i)
                            props[key] = val
                        break
                    if eli == 4:
                        i = m.end()
                        if not nodeDiscard:
                            key = m.group(1)
                            kw = m.group(4)
                            if fastUntagged:
                                val = kwMap[kw]
                            else:
                                val = (
                                    boolCls(True) if kw == "true" else (boolCls(False) if kw == "false" else nullCls())
                                )
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, m.start(4), i)
                            props[key] = val
                        break
                    if eli == 6:
                        i = m.end()
                        if not nodeDiscard:
                            sVal = m.group(6)
                            if fastUntagged:
                                val = sVal
                            else:
                                val = strCls(sVal)
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, m.start(6) - 1, i)
                            args.append(val)
                        break
                    if eli == 7:
                        i = m.end()
                        if not nodeDiscard:
                            numStr = m.group(7)
                            if fastUntagged:
                                av = atomsGet(numStr, unset)
                                val = av if av is not unset else _numFromDecStr(numStr)
                            else:
                                if "." not in numStr and "e" not in numStr and "E" not in numStr:
                                    try:
                                        val = decCls(intFn(numStr, 10), 0)
                                    except ValueError:
                                        val = _decimalFromStr(numStr)
                                else:
                                    val = _decimalFromStr(numStr)
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, m.start(7), i)
                            args.append(val)
                        break
                    if eli == 5:
                        key = m.group(1)
                        valStart = m.end()
                        if s is None:
                            s = Stream(chars, config)
                        val, valEnd = _parseValue(s, valStart, fastUntagged, nodeDiscard)
                        if valEnd < 0:
                            raise ParseError(s, valStart, "Expected value after prop=.")
                        i = valEnd
                        if not nodeDiscard:
                            props[key] = val
                        break
                    tok = m.group(1)
                    if tok in keywords:
                        endPos = m.end()
                        if not nodeDiscard:
                            if fastUntagged:
                                val = kwMap[tok]
                            else:
                                val = (
                                    boolCls(True)
                                    if tok == "true"
                                    else (boolCls(False) if tok == "false" else nullCls())
                                )
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, i, endPos)
                            args.append(val)
                        i = endPos
                        break
                    if s is None:
                        s = Stream(chars, config)
                    if tok.lower() in keywords:
                        raise ParseError(s, i, "KDL keywords are lower-case.")
                    raise ParseError(s, i, "Junk after node, before terminator.")

                if ch2 == '"':
                    strStart = i
                    if s is None:
                        s = Stream(chars, config)
                    strVal, strEnd = _parseEscapedString(s, strStart)
                    if strEnd < n and chars[strEnd] == "=":
                        val, valEnd = _parseValue(s, strEnd + 1, fastUntagged, nodeDiscard)
                        if valEnd < 0:
                            raise ParseError(s, strEnd + 1, "Expected value after prop=.")
                        i = valEnd
                        if not nodeDiscard:
                            props[strVal] = val
                    else:
                        i = strEnd
                        if not nodeDiscard:
                            if fastUntagged:
                                val = strVal
                            else:
                                val = strCls(strVal)
                                if hasValConverters:
                                    val = _finalizeValue(s, val, None, strStart, strEnd)
                            args.append(val)
                    break
                if ch2 == "r" and i + 1 < n and chars[i + 1] in ('"', "#"):
                    j = i + 1
                    while j < n and chars[j] == "#":
                        j += 1
                    if j < n and chars[j] == '"':
                        rawStart = i
                        if s is None:
                            s = Stream(chars, config)
                        rawVal, strEnd = _parseRawString(s, rawStart, j - i - 1)
                        if strEnd < n and chars[strEnd] == "=":
                            val, valEnd = _parseValue(s, strEnd + 1, fastUntagged, nodeDiscard)
                            if valEnd < 0:
                                raise ParseError(s, strEnd + 1, "Expected value after prop=.")
                            i = valEnd
                            if not nodeDiscard:
                                props[rawVal] = val
                        else:
                            i = strEnd
                            if not nodeDiscard:
                                if fastUntagged:
                                    val = rawVal
                                else:
                                    val = RawString(rawVal)
                                    if hasValConverters:
                                        val = _finalizeValue(s, val, None, rawStart, strEnd)
                                args.append(val)
                        break
                if ch2 in digits or (ch2 in ("+", "-") and i + 1 < n and chars[i + 1] in digits):
                    numStart = i
                    if s is None:
                        s = Stream(chars, config)
                    val, valEnd = _parseNumber(s, numStart, fastUntagged, nodeDiscard)
                    i = valEnd
                    if not nodeDiscard:
                        if not fastUntagged and hasValConverters:
                            val = _finalizeValue(s, t.cast("Value", val), None, numStart, valEnd)
                        args.append(val)
                    break
                if ch2 in nlSet:
                    i = i + 2 if (ch2 == "\r" and i + 1 < n and chars[i + 1] == "\n") else i + 1
                    ch2 = "\n"
                    break
                if ch2 in ("t", "f", "n"):
                    mk = _matchKeyword(chars, i)
                    if mk is not None:
                        endPos = mk.end()
                        if not nodeDiscard:
                            kw = mk.group()
                            if fastUntagged:
                                val = kwMap[kw]
                            else:
                                val = (
                                    boolCls(True) if kw == "true" else (boolCls(False) if kw == "false" else nullCls())
                                )
                                if hasValConverters:
                                    if s is None:
                                        s = Stream(chars, config)
                                    val = _finalizeValue(s, val, None, i, endPos)
                            args.append(val)
                        i = endPos
                        break
                if s is None:
                    s = Stream(chars, config)
                if ch2 == "'":
                    raise ParseError(s, i, "KDL strings use double-quotes.")
                raise ParseError(s, i, "Junk after node, before terminator.")

            if enteredChildren or i >= n or ch2 == "\n":
                break

        if not enteredChildren and not nodeDiscard:
            built = nodeCls(name, tag, args, props, [])
            if hasNodeConverters:
                if s is None:
                    s = Stream(chars, config)
                built = _applyNodeConverters(s, built, nodeStart, nameEnd)
            curNodes.append(built)


def parse(input: str, config: t.ParseConfig | None = None) -> Document:
    """Parse a KDL 1.0 document, routing to the fastest tier compatible with `config` and input size."""
    config = config or parsing.defaults
    if not input:
        return Document()
    if not config.valueConverters and not config.nodeConverters:
        if config.nativeUntaggedValues:
            return _parseChunkedNative(input, config)
        if len(input) > 128:
            return _parseChunkedRaw(input, config)
    return _parseDirect(input, config)
