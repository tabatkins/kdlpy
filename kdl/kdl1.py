from __future__ import annotations

import base64
import binascii
import datetime
import decimal
import ipaddress
import itertools
import operator
import re
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
        list[Node],  # parentNodes
        str,  # name
        str | None,  # tag
        list[t.Any],  # args
        dict[str, t.Any],  # props
        bool,  # nodeDiscard
        bool,  # childDiscard
        bool,  # parentDiscard
        int,  # blockPos (exact offset if blockEnd < 0, else chunk pos)
        int,  # blockEnd (chunk endPos if >= 0, else -1)
        int,  # blockTokIdx (token index within chunk if blockEnd >= 0)
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
_ID_CH_OR_Q = r"[^\x00-\x20\\/(){}<>;\[\]=,\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"

_BARE_CORE = rf'(?![+-]?[0-9]|r#*"){_ID_CH}+'
_BARE_PROP_KEY = rf'(?![+-]?[0-9]|r#*"|(?:true|false|null)=){_ID_CH}+'
_DEC = r"[+-]?[0-9][0-9_]*(?:\.[0-9][0-9_]*)?(?:[eE][+-]?[0-9][0-9_]*)?"
_ID_EQ = r"[A-Za-z_][A-Za-z0-9_-]*"
_ID_END = rf"[A-Za-z_][A-Za-z0-9_-]*(?!{_ID_CH_OR_Q})"
_RADIX = r"[+-]?0(?:x[0-9a-fA-F][0-9a-fA-F_]*|o[0-7][0-7_]*|b[01][01_]*)"
_QSTR = r'"[^"\\]*(?:(?:\\["\\/bfnrt]|\\u\{[0-9a-fA-F]{1,6}\})[^"\\]*)*"'
_RAW = r'r(?:"[^"]*"|#"[^"]*(?:"(?!#)[^"]*)*"#|##"[^"]*(?:"(?!##)[^"]*)*"##)(?![#=])'
_SLASH = r"/(?:-|\*[^*]*(?:\*(?!/)[^*]*)*\*/(?=[ \n{]))"

_WS_SET = frozenset(_WS_CHARS)
_NL_SET = frozenset(_NL_CHARS)
_DIGITS = frozenset("0123456789")
_KEYWORDS = frozenset(("true", "false", "null"))
_KW_NATIVE: dict[str, bool | None] = {"true": True, "false": False, "null": None}
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

# 5-group chunked tokenizer for C-speed scanning without Match allocations:
# (propKey, propVal, argOrSym, nlHead, fallback)
# The trailing `(?s:.+)` in `fallback` stops `findall` immediately on any complex/error byte
# and lets us compute its exact offset in O(1) as `endPos - len(fb)`.
_TOK_RE = re.compile(
    r" (?:"
    + rf"({_ID_EQ})=({_DEC}(?![.eExob])|(?:true|false|null)(?!{_ID_CH})|{_QSTR}|{_RADIX})"
    + rf"|(\{{|{_QSTR}(?!=)|{_DEC}(?![.eExob])|\({_ID_EQ}\)(?:{_QSTR}|{_DEC}(?![.eExob]))"
    + rf"|{_RAW}|{_SLASH}|\}}|\\[ \t]*\n[ \t]*(?= )|{_RADIX}|{_ID_END})"
    + r")"
    + rf"|\n(?:[ \t]*(?://[^{_NL_RE}]*)?\n)*[ \t]*(\}}|{_ID_END}|/- {_ID_END}|\({_ID_EQ}\){_ID_END}|)"
    + r"|(\{|;|(?s:.+))",
)
_tokFindall = _TOK_RE.findall
_tokFinditer = _TOK_RE.finditer

_escSub = re.compile(r'\\(["\\/bfnrt]|u\{([0-9a-fA-F]{1,6})\})').sub
_uEscSub = re.compile(r"\\u\{([0-9a-fA-F]{1,6})\}").sub

_dateFromIso = datetime.date.fromisoformat
_timeFromIso = datetime.time.fromisoformat
_dtFromIso = datetime.datetime.fromisoformat
_ipv4Cls = ipaddress.IPv4Address
_ipv6Cls = ipaddress.IPv6Address
_urlparse = urllib.parse.urlparse
_uuidCls = uuid.UUID
_reCompile = re.compile
_a85decode = base64.a85decode
_b64decode = base64.b64decode
_decCls = decimal.Decimal

# Exact regex matchers for fallback, tiny docs, and custom-converter path
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


def _uEscCb(m: re.Match[str]) -> str:
    v = int(m.group(1), 16)
    if v > 0x10FFFF:
        raise ValueError
    return chr(v)


def _unescapeValidated(inner: str) -> str:
    """Unescape a string body already validated by `_QSTR`; raises ValueError on codepoint > 0x10FFFF."""
    if "\\\\" not in inner:
        if '\\"' in inner:
            inner = inner.replace('\\"', '"')
        if "\\n" in inner:
            inner = inner.replace("\\n", "\n")
        if "\\t" in inner:
            inner = inner.replace("\\t", "\t")
        if "\\r" in inner:
            inner = inner.replace("\\r", "\r")
        if "\\/" in inner:
            inner = inner.replace("\\/", "/")
        if "\\b" in inner:
            inner = inner.replace("\\b", "\b")
        if "\\f" in inner:
            inner = inner.replace("\\f", "\f")
        if "\\u" in inner:
            return _uEscSub(_uEscCb, inner)
        return inner
    return _escSub(_unescCb, inner)


def _unescCb(m: re.Match[str]) -> str:
    h = m.group(2)
    if h is not None:
        v = int(h, 16)
        if v > 0x10FFFF:
            raise ValueError
        return chr(v)
    return _SIMPLE_ESCAPES[m.group(1)]


def _skipBlockComment(s: Stream, start: int) -> int:
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
    if "_" in raw:
        raw = raw.replace("_", "")
    fval = float(raw)
    if fval == 0.0 and raw[0] == "-" and "." not in raw:
        ePos = raw.find("e")
        if ePos == -1:
            ePos = raw.find("E")
        if ePos == -1 or not raw[:ePos].strip("+-0"):
            return 0.0
    return fval


def _numFromDecStr(raw: str) -> float | int:
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


def _parseNumber(s: Stream, start: int, native: bool, discard: bool) -> tuple[t.Any, int]:
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
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 16) * sign
            return (intVal if native else Hex(intVal), m.end())
        if prefix == "o":
            m = _matchOctDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected octal digit after 0o, got junk.")
            if discard:
                return (None, m.end())
            raw = m.group()
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 8) * sign
            return (intVal if native else Octal(intVal), m.end())
        if prefix == "b":
            m = _matchBinDigits(chars, i + 2)
            if m is None:
                raise ParseError(s, i + 2, "Expected binary digit after 0b, got junk.")
            if discard:
                return (None, m.end())
            raw = m.group()
            intVal = int(raw.replace("_", "") if "_" in raw else raw, 2) * sign
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


def _convertTaggedStringFast(tag: str, text: str) -> t.Any:
    match tag:
        case "date":
            try:
                return _dateFromIso(text)
            except ValueError:
                return _UNSET
        case "ipv4":
            try:
                return _ipv4Cls(text)
            except ipaddress.AddressValueError:
                return _UNSET
        case "url" | "url-reference" | "irl" | "irl-reference":
            try:
                return _urlparse(text)
            except ValueError:
                return _UNSET
        case "date-time":
            try:
                return _dtFromIso(text)
            except ValueError:
                return _UNSET
        case "time":
            try:
                return _timeFromIso(text)
            except ValueError:
                return _UNSET
        case "ipv6":
            try:
                return _ipv6Cls(text)
            except ipaddress.AddressValueError:
                return _UNSET
        case "uuid":
            try:
                return _uuidCls(text)
            except ValueError:
                return _UNSET
        case "regex":
            try:
                return _reCompile(text)
            except re.error:
                return _UNSET
        case "base64":
            try:
                return _b64decode(text.encode("utf-8"), validate=True)
            except (binascii.Error, ValueError):
                return _UNSET
        case "base85":
            try:
                return _a85decode(text.encode("utf-8"), adobe=text.startswith("<~") and text.endswith("~>"))
            except ValueError:
                return _UNSET
        case "decimal":
            try:
                return _decCls(text)
            except decimal.InvalidOperation:
                return _UNSET
        case _:
            return String(text, tag)


def _convertTaggedDecFast(tag: str, raw: str) -> t.Any:
    bounds = _INT_TAG_BOUNDS.get(tag)
    if bounds is not None:
        cleaned = raw.replace("_", "") if "_" in raw else raw
        if "." not in cleaned and "e" not in cleaned and "E" not in cleaned:
            try:
                iv = int(cleaned, 10)
            except ValueError:
                return _UNSET
            if bounds[0] <= iv < bounds[1]:
                return iv
            return _UNSET
        try:
            dv = _decCls(cleaned)
        except decimal.InvalidOperation:
            return _UNSET
        if dv.is_finite() and dv == dv.to_integral_value() and bounds[0] <= dv < bounds[1]:
            return int(dv)
        return _UNSET
    if tag in ("f32", "f64"):
        return _floatFromDecStr(raw)
    if tag in ("decimal64", "decimal128"):
        try:
            return _decCls(raw.replace("_", "") if "_" in raw else raw)
        except decimal.InvalidOperation:
            return _UNSET
    d = _decimalFromStr(raw)
    d.tag = tag
    return d


def _finalizeValue(s: Stream, val: Value, tag: str | None, valStart: int, valEnd: int) -> t.Any:
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
    target = pos + chunkSize
    if target >= n:
        return n
    nlPos = chars.find("\n", target, target + 512)
    if nlPos != -1:
        return nlPos
    spPos = chars.find(" ", target, target + 512)
    if spPos != -1:
        return spPos
    # Giant token (> 512 bytes without `\n` or ` `, e.g. long string or comment):
    # cut before it if possible, else return -1 to handle it directly in C via fallback.
    prevNl = chars.rfind("\n", pos + 1, target)
    if prevNl != -1:
        return prevNl
    prevSp = chars.rfind(" ", pos + 1, target)
    if prevSp != -1:
        return prevSp
    return -1


def _tokenOffset(chars: str, pos: int, endPos: int, tokIdx: int) -> int:
    m = next(itertools.islice(_tokFinditer(chars, pos, endPos), tokIdx, tokIdx + 1))
    return m.start()


def _unclosedBlockPos(chars: str, frame: _FastFrame) -> int:
    bPos = frame[8]
    bEnd = frame[9]
    if bEnd < 0:
        return bPos
    m = next(itertools.islice(_tokFinditer(chars, bPos, bEnd), frame[10], frame[10] + 1))
    # For `' {'` token, `{` is at start + 1; for bare `'{'`, at start.
    st = m.start()
    return st + 1 if chars[st] == " " else st


def _handleChunkFallback(
    s: Stream,
    i: int,
    *,
    nativeUntagged: bool,
    nativeTagged: bool,
    state: int,
    name: str,
    tag: str | None,
    args: list[t.Any] | None,
    props: dict[str, t.Any] | None,
    nodeDiscard: bool,
    curNodes: list[Node],
    curDiscard: bool,
    stack: list[_FastFrame],
) -> tuple[
    int,  # new pos (or -1 to return Document)
    int,  # new state
    str,  # name
    str | None,  # tag
    list[t.Any] | None,  # args
    dict[str, t.Any] | None,  # props
    bool,  # nodeDiscard
    list[Node],  # curNodes
    bool,  # curDiscard
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
            return (mh.end(), 1, cand, None, None, None, curDiscard, curNodes, curDiscard)
        if li == 3:
            if not stack:
                raise ParseError(s, mh.start(3), "Expected a node")
            parentNodes, pName, pTag, pArgs, pProps, pNodeDiscard, pChildDiscard, pParentDiscard, _, _, _ = stack.pop()
            if not pNodeDiscard:
                parentNodes.append(Node(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes))
            return (mh.end(), 2, "", None, None, None, False, parentNodes, pParentDiscard)
        if li is None:
            end = mh.end()
            if end >= n:
                if stack:
                    raise ParseError(
                        s,
                        _unclosedBlockPos(chars, stack[-1]),
                        "Hit EOF while searching for end of child list",
                    )
                return (-1, 0, "", None, None, None, False, curNodes, curDiscard)
            raise ParseError(
                s,
                end,
                "Junk between end of child list and closing }" if stack else "Expected a node",
            )
        if li == 2:
            return (mh.end(), 1, mh.group(2), None, None, None, curDiscard, curNodes, curDiscard)
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
            return (nameEnd, 1, nameOrNone, newTag, None, None, curDiscard, curNodes, curDiscard)
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
            return (nameEnd, 1, nameOrNone, newTag, None, None, True, curNodes, curDiscard)
        if li == 6:
            return (_skipBlockComment(s, mh.start(6)), 0, "", None, None, None, False, curNodes, curDiscard)
        if li == 7:
            qName, qEnd = _parseEscapedString(s, mh.start(7))
            return (qEnd, 1, qName, None, None, None, curDiscard, curNodes, curDiscard)
        rawStart = mh.start(8)
        rName, rEnd = _parseRawString(s, rawStart, mh.end() - rawStart - 2)
        return (rEnd, 1, rName, None, None, None, curDiscard, curNodes, curDiscard)

    if state == 2:
        if i < n:
            chEnd = chars[i]
            if chEnd in wsSet or chEnd == "\\" or (chEnd == "/" and i + 1 < n and chars[i + 1] == "*"):
                i = _skipNodespace(s, i)
            i = _skipTerminator(s, i)
        return (i, 0, "", None, None, None, False, curNodes, curDiscard)

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
            finalArgs: list[t.Any] = [] if (nodeDiscard or args is None) else args
            finalProps: dict[str, t.Any] = {} if (nodeDiscard or props is None) else props
            stack.append((curNodes, name, tag, finalArgs, finalProps, nodeDiscard, nodeDiscard, curDiscard, i, -1, 0))
            return (i + 1, 0, "", None, None, None, False, [], nodeDiscard)
        if ch == "/" and i + 1 < n and chars[i + 1] == "-":
            j = _skipNodespace(s, i + 2)
            if j < n and chars[j] == "{":
                finalArgs = [] if (nodeDiscard or args is None) else args
                finalProps = {} if (nodeDiscard or props is None) else props
                stack.append((curNodes, name, tag, finalArgs, finalProps, nodeDiscard, True, curDiscard, i, -1, 0))
                return (j + 1, 0, "", None, None, None, False, [], True)
            raise ParseError(s, i, "Junk after node, before terminator.")
        i = _skipTerminator(s, i)
        if not nodeDiscard:
            curNodes.append(Node(name, tag, [] if args is None else args, {} if props is None else props, []))
        return (i, 0, "", None, None, None, False, curNodes, curDiscard)

    if i >= n:
        if not nodeDiscard:
            curNodes.append(Node(name, tag, [] if args is None else args, {} if props is None else props, []))
        return (n, 0, "", None, None, None, False, curNodes, curDiscard)

    ch2 = chars[i]
    if ch2 == "{":
        finalArgs = [] if (nodeDiscard or args is None) else args
        finalProps = {} if (nodeDiscard or props is None) else props
        stack.append((curNodes, name, tag, finalArgs, finalProps, nodeDiscard, nodeDiscard, curDiscard, i, -1, 0))
        return (i + 1, 0, "", None, None, None, False, [], nodeDiscard)
    if ch2 in ("\n", ";") or ch2 in nlSet or (ch2 == "/" and i + 1 < n and chars[i + 1] == "/"):
        i = _skipTerminator(s, i)
        if not nodeDiscard:
            curNodes.append(Node(name, tag, [] if args is None else args, {} if props is None else props, []))
        return (i, 0, "", None, None, None, False, curNodes, curDiscard)
    if ch2 == "/" and i + 1 < n and chars[i + 1] == "-":
        sdStart = i
        j = _skipNodespace(s, i + 2)
        if j < n and chars[j] == "{":
            finalArgs = [] if (nodeDiscard or args is None) else args
            finalProps = {} if (nodeDiscard or props is None) else props
            stack.append((curNodes, name, tag, finalArgs, finalProps, nodeDiscard, True, curDiscard, sdStart, -1, 0))
            return (j + 1, 0, "", None, None, None, False, [], True)
        entEnd = _parseDiscardedEntity(s, j)
        if entEnd < 0:
            raise ParseError(s, sdStart, "Junk after node, before terminator.")
        return (entEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)

    def _addProp(pk: str, pv: t.Any) -> None:
        nonlocal props
        if props is None:
            props = {pk: pv}
        else:
            props[pk] = pv

    def _addArg(av: t.Any) -> None:
        nonlocal args
        if args is None:
            args = [av]
        else:
            args.append(av)

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
                _addProp(key, pval)
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
                _addProp(key, val)
            return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if eli == 6:
            if not nodeDiscard:
                sVal = m.group(6)
                _addArg(sVal if nativeUntagged else String(sVal))
            return (m.end(), 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if eli == 7:
            if not nodeDiscard:
                rawNum = m.group(7)
                _addArg(_numFromDecStr(rawNum) if nativeUntagged else _decimalFromStr(rawNum))
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
                _addArg(val)
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
                _addProp(strVal, val)
            return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
        if not nodeDiscard:
            _addArg(strVal if nativeUntagged else String(strVal))
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
                    _addProp(rawVal, val)
                return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)
            if not nodeDiscard:
                _addArg(rawVal if nativeUntagged else RawString(rawVal))
            return (strEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)

    val, valEnd = (
        _parseValueFast(s, i, nodeDiscard, nativeTagged) if nativeUntagged else _parseValue(s, i, False, nodeDiscard)
    )
    if valEnd < 0:
        raise ParseError(s, i, "Junk after node, before terminator.")
    if not nodeDiscard:
        _addArg(val)
    return (valEnd, 1, name, tag, args, props, nodeDiscard, curNodes, curDiscard)


def _parseChunkedNative(s: Stream) -> Document:
    """Specialized chunked C-regex parser for nativeUntaggedValues=True with no custom converters."""
    chars = s.chars
    n = s.length
    nativeTagged = s.config.nativeTaggedValues
    findall = _tokFindall
    lengthHint = _lengthHint
    kwMap = _KW_NATIVE
    kwGet = kwMap.get
    keywords = _KEYWORDS
    digits = _DIGITS
    unset = _UNSET
    nodeCls = Node
    strCls = String
    floatFn = float
    intFn = int
    unescFn = _unescapeValidated

    curNodes: list[Node] = []
    curDiscard = False
    stack: list[_FastFrame] = []
    stackPush = stack.append
    stackPop = stack.pop

    state = 0
    name = ""
    tag: str | None = None
    args: list[t.Any] | None = None
    props: dict[str, t.Any] | None = None
    nodeDiscard = False
    nextEntDiscard = False
    sdTokIdx = 0

    pos = 0
    if n > 0:
        mh0 = _matchNodeHead(chars, 0)
        assert mh0 is not None
        li0 = mh0.lastindex
        if li0 == 1:
            cand0 = mh0.group(1)
            if cand0 not in keywords:
                name = cand0
                pos = mh0.end()
                state = 1
        elif li0 is None and mh0.end() >= n:
            return Document(curNodes)

    chunkSize = 512
    while pos < n:
        endPos = _pickChunkEnd(chars, pos, n, chunkSize)
        if endPos < 0:
            # Giant single token (> 512 bytes with no space/newline): handle 1 exact step directly.
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
            pos = newPos
            continue

        toks = findall(chars, pos, endPos)
        nToks = len(toks)
        tokIter = iter(toks)
        failIdx = -1
        failOffset = -1

        for k, pv, arg, nh, fb in tokIter:
            if k:
                if state != 1 or k in keywords:
                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                    nextEntDiscard = False
                    break
                if nodeDiscard or nextEntDiscard:
                    nextEntDiscard = False
                    if pv[0] == '"' and "\\" in pv:
                        try:
                            unescFn(pv[1:-1])
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    continue
                vc = pv[0]
                if "1" <= vc <= "9":
                    if "." not in pv and "e" not in pv and "E" not in pv:
                        try:
                            pval: t.Any = intFn(pv, 10)
                        except ValueError:
                            pval = _numFromDecStr(pv)
                    elif "." in pv:
                        try:
                            pval = floatFn(pv)
                        except ValueError:
                            pval = floatFn(pv.replace("_", ""))
                    else:
                        pval = _numFromDecStr(pv)
                elif vc == '"':
                    inner = pv[1:-1]
                    if "\\" in inner:
                        try:
                            inner = unescFn(inner)
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    pval = inner
                elif vc in ("t", "f", "n"):
                    pval = kwMap[pv]
                elif len(pv) >= 2 and (
                    pv[1] in ("x", "o", "b") or (vc in ("+", "-") and len(pv) >= 3 and pv[2] in ("x", "o", "b"))
                ):
                    try:
                        pval = intFn(pv, 0)
                    except ValueError:
                        pval = intFn(pv.replace("_", ""), 0)
                else:
                    pval = _numFromDecStr(pv)
                if props is None:
                    props = {k: pval}
                else:
                    props[k] = pval
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
                        curNodes.append(
                            nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                        )
                    parentNodes, pName, pTag, pArgs, pProps, pNodeDiscard, pChildDiscard, pParentDiscard, _, _, _ = (
                        stackPop()
                    )
                    if not pNodeDiscard:
                        parentNodes.append(nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes))
                    curNodes = parentNodes
                    curDiscard = pParentDiscard
                    state = 2
                    continue
                hc0 = nh[0]
                if hc0 not in ("/", "("):
                    if nh in keywords:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    nTag: str | None = None
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
                    curNodes.append(
                        nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                    )
                name = nName
                tag = nTag
                args = None
                props = None
                nodeDiscard = nDisc
                state = 1
                continue

            if arg:
                ac0 = arg[0]
                if state != 1:
                    if state == 0:
                        if ac0 == "}":
                            if not stack:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            (
                                parentNodes,
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
                            ) = stackPop()
                            if not pNodeDiscard:
                                parentNodes.append(
                                    nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes),
                                )
                            curNodes = parentNodes
                            curDiscard = pParentDiscard
                            state = 2
                            continue
                        if (
                            ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                            and ac0 not in digits
                            and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                            and arg not in keywords
                        ):
                            name = arg
                            tag = None
                            args = None
                            props = None
                            nodeDiscard = curDiscard
                            state = 1
                            continue
                    failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if ac0 == "{":
                    finalArgs: list[t.Any] = [] if (nodeDiscard or args is None) else args
                    finalProps: dict[str, t.Any] = {} if (nodeDiscard or props is None) else props
                    childDisc = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    stackPush(
                        (
                            curNodes,
                            name,
                            tag,
                            finalArgs,
                            finalProps,
                            nodeDiscard,
                            childDisc,
                            curDiscard,
                            pos,
                            endPos,
                            nToks - 1 - lengthHint(tokIter),
                        ),
                    )
                    curNodes = []
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
                        if args is None:
                            args = [inner]
                        else:
                            args.append(inner)
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
                                conv = _convertTaggedStringFast(tName, inner)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                val: t.Any = conv
                            else:
                                val = strCls(inner, tName)
                            if args is None:
                                args = [val]
                            else:
                                args.append(val)
                        continue
                    nextEntDiscard = False
                    if not entDiscard:
                        if nativeTagged:
                            conv = _convertTaggedDecFast(tName, vStr)
                            if conv is unset:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            val = conv
                        else:
                            dObj = _decimalFromStr(vStr)
                            dObj.tag = tName
                            val = dObj
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                    nextEntDiscard = False
                    if not entDiscard:
                        q = arg.find('"', 1)
                        h = q - 1
                        rText = arg[q + 1 : len(arg) - 1 - h]
                        if args is None:
                            args = [rText]
                        else:
                            args.append(rText)
                    continue
                if "1" <= ac0 <= "9":
                    nextEntDiscard = False
                    if not entDiscard:
                        if "." not in arg and "e" not in arg and "E" not in arg:
                            try:
                                val = intFn(arg, 10)
                            except ValueError:
                                val = _numFromDecStr(arg)
                        elif "." in arg:
                            try:
                                val = floatFn(arg)
                            except ValueError:
                                val = floatFn(arg.replace("_", ""))
                        else:
                            val = _numFromDecStr(arg)
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and arg[1] in digits):
                    nextEntDiscard = False
                    if not entDiscard:
                        if len(arg) >= 2 and (
                            arg[1] in ("x", "o", "b")
                            or (ac0 in ("+", "-") and len(arg) >= 3 and arg[2] in ("x", "o", "b"))
                        ):
                            try:
                                val = intFn(arg, 0)
                            except ValueError:
                                val = intFn(arg.replace("_", ""), 0)
                        else:
                            val = _numFromDecStr(arg)
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                kwVal = kwGet(arg, unset)
                if kwVal is not unset:
                    nextEntDiscard = False
                    if not entDiscard:
                        if args is None:
                            args = [kwVal]
                        else:
                            args.append(kwVal)
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
                            curNodes.append(
                                nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                            )
                        state = 0
                        continue
                    if state == 2:
                        state = 0
                        continue
                elif fb == "{" and state == 1:
                    finalArgs = [] if (nodeDiscard or args is None) else args
                    finalProps = {} if (nodeDiscard or props is None) else props
                    childDisc = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    stackPush(
                        (
                            curNodes,
                            name,
                            tag,
                            finalArgs,
                            finalProps,
                            nodeDiscard,
                            childDisc,
                            curDiscard,
                            pos,
                            endPos,
                            nToks - 1 - lengthHint(tokIter),
                        ),
                    )
                    curNodes = []
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

            # Empty newline (all 5 groups == "")
            if nextEntDiscard:
                failIdx = sdTokIdx
                nextEntDiscard = False
                break
            if state == 1 and not nodeDiscard:
                curNodes.append(nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []))
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
        pos = newPos

    if state == 1 and not nodeDiscard:
        curNodes.append(nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []))
    if stack:
        raise ParseError(s, _unclosedBlockPos(chars, stack[-1]), "Hit EOF while searching for end of child list")
    return Document(curNodes)


def _parseChunkedRaw(s: Stream) -> Document:
    """Specialized chunked C-regex parser for nativeUntaggedValues=False with no custom converters."""
    chars = s.chars
    n = s.length
    nativeTagged = s.config.nativeTaggedValues
    findall = _tokFindall
    lengthHint = _lengthHint
    keywords = _KEYWORDS
    digits = _DIGITS
    unset = _UNSET
    nodeCls = Node
    strCls = String
    rawStrCls = RawString
    boolCls = Bool
    nullCls = Null
    hexCls = Hex
    octCls = Octal
    binCls = Binary
    intFn = int
    unescFn = _unescapeValidated

    curNodes: list[Node] = []
    curDiscard = False
    stack: list[_FastFrame] = []
    stackPush = stack.append
    stackPop = stack.pop

    state = 0
    name = ""
    tag: str | None = None
    args: list[t.Any] | None = None
    props: dict[str, t.Any] | None = None
    nodeDiscard = False
    nextEntDiscard = False
    sdTokIdx = 0

    pos = 0
    if n > 0:
        mh0 = _matchNodeHead(chars, 0)
        assert mh0 is not None
        li0 = mh0.lastindex
        if li0 == 1:
            cand0 = mh0.group(1)
            if cand0 not in keywords:
                name = cand0
                pos = mh0.end()
                state = 1
        elif li0 is None and mh0.end() >= n:
            return Document(curNodes)

    chunkSize = 512
    while pos < n:
        endPos = _pickChunkEnd(chars, pos, n, chunkSize)
        if endPos < 0:
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
            pos = newPos
            continue

        toks = findall(chars, pos, endPos)
        nToks = len(toks)
        tokIter = iter(toks)
        failIdx = -1
        failOffset = -1

        for k, pv, arg, nh, fb in tokIter:
            if k:
                if state != 1 or k in keywords:
                    failIdx = sdTokIdx if nextEntDiscard else (nToks - 1 - lengthHint(tokIter))
                    nextEntDiscard = False
                    break
                if nodeDiscard or nextEntDiscard:
                    nextEntDiscard = False
                    if pv[0] == '"' and "\\" in pv:
                        try:
                            unescFn(pv[1:-1])
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    continue
                vc = pv[0]
                if "1" <= vc <= "9":
                    pval: t.Any = _decimalFromStr(pv)
                elif vc == '"':
                    inner = pv[1:-1]
                    if "\\" in inner:
                        try:
                            inner = unescFn(inner)
                        except ValueError:
                            failIdx = nToks - 1 - lengthHint(tokIter)
                            break
                    pval = strCls(inner)
                elif vc in ("t", "f", "n"):
                    pval = boolCls(True) if pv == "true" else (boolCls(False) if pv == "false" else nullCls())
                elif len(pv) >= 2 and (
                    pv[1] in ("x", "o", "b") or (vc in ("+", "-") and len(pv) >= 3 and pv[2] in ("x", "o", "b"))
                ):
                    iv = intFn(pv.replace("_", "") if "_" in pv else pv, 0)
                    rc = pv[2] if vc in ("+", "-") else pv[1]
                    pval = hexCls(iv) if rc == "x" else (octCls(iv) if rc == "o" else binCls(iv))
                else:
                    pval = _decimalFromStr(pv)
                if props is None:
                    props = {k: pval}
                else:
                    props[k] = pval
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
                        curNodes.append(
                            nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                        )
                    parentNodes, pName, pTag, pArgs, pProps, pNodeDiscard, pChildDiscard, pParentDiscard, _, _, _ = (
                        stackPop()
                    )
                    if not pNodeDiscard:
                        parentNodes.append(nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes))
                    curNodes = parentNodes
                    curDiscard = pParentDiscard
                    state = 2
                    continue
                hc0 = nh[0]
                if hc0 not in ("/", "("):
                    if nh in keywords:
                        failIdx = nToks - 1 - lengthHint(tokIter)
                        break
                    nTag: str | None = None
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
                    curNodes.append(
                        nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                    )
                name = nName
                tag = nTag
                args = None
                props = None
                nodeDiscard = nDisc
                state = 1
                continue

            if arg:
                ac0 = arg[0]
                if state != 1:
                    if state == 0:
                        if ac0 == "}":
                            if not stack:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            (
                                parentNodes,
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
                            ) = stackPop()
                            if not pNodeDiscard:
                                parentNodes.append(
                                    nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes),
                                )
                            curNodes = parentNodes
                            curDiscard = pParentDiscard
                            state = 2
                            continue
                        if (
                            ac0 not in ('"', "(", "{", "/", "\\", "+", "-")
                            and ac0 not in digits
                            and not (ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"))
                            and arg not in keywords
                        ):
                            name = arg
                            tag = None
                            args = None
                            props = None
                            nodeDiscard = curDiscard
                            state = 1
                            continue
                    failIdx = nToks - 1 - lengthHint(tokIter)
                    break

                if ac0 == "{":
                    finalArgs: list[t.Any] = [] if (nodeDiscard or args is None) else args
                    finalProps: dict[str, t.Any] = {} if (nodeDiscard or props is None) else props
                    childDisc = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    stackPush(
                        (
                            curNodes,
                            name,
                            tag,
                            finalArgs,
                            finalProps,
                            nodeDiscard,
                            childDisc,
                            curDiscard,
                            pos,
                            endPos,
                            nToks - 1 - lengthHint(tokIter),
                        ),
                    )
                    curNodes = []
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
                        sv = strCls(inner)
                        if args is None:
                            args = [sv]
                        else:
                            args.append(sv)
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
                                conv = _convertTaggedStringFast(tName, inner)
                                if conv is unset:
                                    failIdx = nToks - 1 - lengthHint(tokIter)
                                    break
                                val: t.Any = conv
                            else:
                                val = strCls(inner, tName)
                            if args is None:
                                args = [val]
                            else:
                                args.append(val)
                        continue
                    nextEntDiscard = False
                    if not entDiscard:
                        if nativeTagged:
                            conv = _convertTaggedDecFast(tName, vStr)
                            if conv is unset:
                                failIdx = nToks - 1 - lengthHint(tokIter)
                                break
                            val = conv
                        else:
                            dObj = _decimalFromStr(vStr)
                            dObj.tag = tName
                            val = dObj
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                if ac0 == "r" and len(arg) > 1 and arg[1] in ('"', "#"):
                    nextEntDiscard = False
                    if not entDiscard:
                        q = arg.find('"', 1)
                        h = q - 1
                        rv = rawStrCls(arg[q + 1 : len(arg) - 1 - h])
                        if args is None:
                            args = [rv]
                        else:
                            args.append(rv)
                    continue
                if "1" <= ac0 <= "9":
                    nextEntDiscard = False
                    if not entDiscard:
                        val = _decimalFromStr(arg)
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                if ac0 == "0" or (ac0 in ("+", "-") and len(arg) > 1 and arg[1] in digits):
                    nextEntDiscard = False
                    if not entDiscard:
                        if len(arg) >= 2 and (
                            arg[1] in ("x", "o", "b")
                            or (ac0 in ("+", "-") and len(arg) >= 3 and arg[2] in ("x", "o", "b"))
                        ):
                            iv = intFn(arg.replace("_", "") if "_" in arg else arg, 0)
                            rc = arg[2] if ac0 in ("+", "-") else arg[1]
                            val = hexCls(iv) if rc == "x" else (octCls(iv) if rc == "o" else binCls(iv))
                        else:
                            val = _decimalFromStr(arg)
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
                    continue
                if arg in keywords:
                    nextEntDiscard = False
                    if not entDiscard:
                        val = boolCls(True) if arg == "true" else (boolCls(False) if arg == "false" else nullCls())
                        if args is None:
                            args = [val]
                        else:
                            args.append(val)
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
                            curNodes.append(
                                nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []),
                            )
                        state = 0
                        continue
                    if state == 2:
                        state = 0
                        continue
                elif fb == "{" and state == 1:
                    finalArgs = [] if (nodeDiscard or args is None) else args
                    finalProps = {} if (nodeDiscard or props is None) else props
                    childDisc = nodeDiscard or nextEntDiscard
                    nextEntDiscard = False
                    stackPush(
                        (
                            curNodes,
                            name,
                            tag,
                            finalArgs,
                            finalProps,
                            nodeDiscard,
                            childDisc,
                            curDiscard,
                            pos,
                            endPos,
                            nToks - 1 - lengthHint(tokIter),
                        ),
                    )
                    curNodes = []
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
                curNodes.append(nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []))
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
        pos = newPos

    if state == 1 and not nodeDiscard:
        curNodes.append(nodeCls(name, tag, [] if args is None else args, {} if props is None else props, []))
    if stack:
        raise ParseError(s, _unclosedBlockPos(chars, stack[-1]), "Hit EOF while searching for end of child list")
    return Document(curNodes)


def _applyNodeConverters(s: Stream, node: Node, nodeStart: int, nameEnd: int) -> Node:
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


def _parseDirect(s: Stream) -> Document:
    """Monolithic iterative parser for tiny inputs (<= 128 chars) and configs with custom converters."""
    chars = s.chars
    n = s.length
    config = s.config
    fastUntagged = s.fastUntagged
    hasNodeConverters = bool(config.nodeConverters)
    hasValConverters = bool(config.valueConverters)
    matchHead = _matchNodeHead
    matchEntry = _matchEntry
    kwMap = _KW_NATIVE
    keywords = _KEYWORDS
    wsSet = _WS_SET
    nlSet = _NL_SET
    digits = _DIGITS
    nodeCls = Node
    strCls = String
    boolCls = Bool
    nullCls = Null

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
                raise ParseError(
                    s,
                    nodeStart,
                    "Junk between end of child list and closing }" if stack else "Expected a node",
                )
            i = mh.end()
            nameEnd = i
        elif li == 3:
            if not stack:
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
                    if chEnd in wsSet or chEnd == "\\" or (chEnd == "/" and i + 1 < n and chars[i + 1] == "*"):
                        i = _skipNodespace(s, i)
                    i = _skipTerminator(s, i)
            if not pNodeDiscard:
                built = nodeCls(pName, pTag, pArgs, pProps, [] if pChildDiscard else curNodes)
                if hasNodeConverters:
                    built = _applyNodeConverters(s, built, pStart, pNameEnd)
                parentNodes.append(built)
            curNodes = parentNodes
            curDiscard = pParentDiscard
            continue
        elif li is None:
            i = mh.end()
            if i >= n:
                if stack:
                    raise ParseError(s, stack[-1][10], "Hit EOF while searching for end of child list")
                return Document(curNodes)
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
            i = _skipBlockComment(s, mh.start(6))
            continue
        elif li == 7:
            nodeStart = mh.start(7)
            name, i = _parseEscapedString(s, nodeStart)
            nameEnd = i
        else:
            nodeStart = mh.start(8)
            name, i = _parseRawString(s, nodeStart, mh.end() - nodeStart - 2)
            nameEnd = i

        if i >= n:
            if not nodeDiscard:
                built = nodeCls(name, tag, [], {}, [])
                if hasNodeConverters:
                    built = _applyNodeConverters(s, built, nodeStart, nameEnd)
                curNodes.append(built)
            continue
        ch0 = chars[i]
        if ch0 in ("\n", ";"):
            i += 1
            if not nodeDiscard:
                built = nodeCls(name, tag, [], {}, [])
                if hasNodeConverters:
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
                nextI = _skipNodespace(s, i)
                if nextI == i:
                    i = _skipTerminator(s, i)
                    break
                i = nextI
                if i >= n:
                    break
            elif ch == "/" and i + 1 < n and chars[i + 1] == "-":
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
                    val, valEnd = _parseValue(s, i, fastUntagged, nodeDiscard)
                    if valEnd < 0:
                        raise ParseError(s, i, "Junk after node, before terminator.")
                    i = valEnd
                    if not nodeDiscard:
                        args.append(val)
                    break
                if ch2 in wsSet or ch2 == "\\" or (ch2 == "/" and i + 1 < n and chars[i + 1] == "*"):
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
                            key, numStr = t.cast("tuple[str, str]", m.group(1, 3))
                            if fastUntagged:
                                val = _numFromDecStr(numStr)
                            else:
                                val = _decimalFromStr(numStr)
                                if hasValConverters:
                                    val = _finalizeValue(s, val, None, m.start(3), i)
                            props[key] = val
                        break
                    if eli == 2:
                        i = m.end()
                        if not nodeDiscard:
                            key, sVal = t.cast("tuple[str, str]", m.group(1, 2))
                            if fastUntagged:
                                val = sVal
                            else:
                                val = strCls(sVal)
                                if hasValConverters:
                                    val = _finalizeValue(s, val, None, m.start(2) - 1, i)
                            props[key] = val
                        break
                    if eli == 4:
                        i = m.end()
                        if not nodeDiscard:
                            key, kw = t.cast("tuple[str, str]", m.group(1, 4))
                            if fastUntagged:
                                val = kwMap[kw]
                            else:
                                val = (
                                    boolCls(True) if kw == "true" else (boolCls(False) if kw == "false" else nullCls())
                                )
                                if hasValConverters:
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
                                    val = _finalizeValue(s, val, None, m.start(6) - 1, i)
                            args.append(val)
                        break
                    if eli == 7:
                        i = m.end()
                        if not nodeDiscard:
                            if fastUntagged:
                                val = _numFromDecStr(m.group(7))
                            else:
                                val = _decimalFromStr(m.group(7))
                                if hasValConverters:
                                    val = _finalizeValue(s, val, None, m.start(7), i)
                            args.append(val)
                        break
                    if eli == 5:
                        key = m.group(1)
                        valStart = m.end()
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
                                    val = _finalizeValue(s, val, None, i, endPos)
                            args.append(val)
                        i = endPos
                        break
                    if tok.lower() in keywords:
                        raise ParseError(s, i, "KDL keywords are lower-case.")
                    raise ParseError(s, i, "Junk after node, before terminator.")

                if ch2 == '"':
                    strStart = i
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
                                    val = _finalizeValue(s, val, None, i, endPos)
                            args.append(val)
                        i = endPos
                        break
                if ch2 == "'":
                    raise ParseError(s, i, "KDL strings use double-quotes.")
                raise ParseError(s, i, "Junk after node, before terminator.")

            if enteredChildren or i >= n or ch2 == "\n":
                break

        if not enteredChildren and not nodeDiscard:
            built = nodeCls(name, tag, args, props, [])
            if hasNodeConverters:
                built = _applyNodeConverters(s, built, nodeStart, nameEnd)
            curNodes.append(built)


def parse(input: str, config: t.ParseConfig | None = None) -> Document:
    config = config or parsing.defaults
    s = Stream(input, config)
    if s.length > 128 and not config.valueConverters and not config.nodeConverters:
        if config.nativeUntaggedValues:
            return _parseChunkedNative(s)
        return _parseChunkedRaw(s)
    return _parseDirect(s)
