from __future__ import annotations

import re

from .chars import DIGITS, IDENT_CHAR_RE_STR, NL_RE, WS_RE

KDL1_KEYWORDS = frozenset(("true", "false", "null"))
_KDL1_HASH_KEYWORDS = ("#true", "#false", "#null", "#inf", "#-inf", "#nan")

_matchVersionMarker = re.compile(
    rf"/-[{WS_RE}]*kdl-version[{WS_RE}]+([12])[{WS_RE}]*(?:(?://[^{NL_RE}]*)?(?:\r\n|[{NL_RE}]|$)|;)",
).match
_matchKdl2IdentChar = re.compile(IDENT_CHAR_RE_STR).match
_matchKdl1Ident = re.compile(
    r'[^\x00-\x20\\/(){}<>;\[\]=,"\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+',
).match
_matchKdl1Ws = re.compile(rf"[{WS_RE}\ufeff]+").match


def _afterSlashdash(chars: str, pos: int, n: int) -> int:
    """If `pos` starts a `/-` token, return the index after `/-` and any following KDL1 whitespace; else -1."""
    if pos + 1 >= n or chars[pos] != "/" or chars[pos + 1] != "-":
        return -1
    j = pos + 2
    if j < n:
        m = _matchKdl1Ws(chars, j)
        if m is not None:
            j = m.end()
    return j


def detectVersionMarker(chars: str, pos: int) -> int | None:
    """Return 1 or 2 if a leading `/- kdl-version <n>` marker starts at `pos`, else None."""
    m = _matchVersionMarker(chars, pos)
    if m is None:
        return None
    return 1 if m.group(1) == "1" else 2


def isRawStringAt(chars: str, pos: int, n: int) -> bool:
    """Return True if `pos` is the `"` or `#` immediately following a standalone `r` prefix of a KDL1 raw string."""
    if pos < 1 or chars[pos - 1] != "r":
        return False
    if pos >= 2 and _matchKdl2IdentChar(chars[pos - 2]) is not None:
        if not (pos >= 3 and chars[pos - 2] == "-" and chars[pos - 3] == "/"):
            return False
    j = pos
    while j < n and chars[j] == "#":
        j += 1
    return j < n and chars[j] == '"'


def startsWithDotIdent(chars: str, pos: int, n: int) -> bool:
    """Return True if `chars[pos:]` starts with `[+-]?.[0-9]`, a bare ident allowed in KDL1 but not KDL2."""
    if pos >= n:
        return False
    i = pos + 1 if chars[pos] in "+-" else pos
    return i + 1 < n and chars[i] == "." and chars[i + 1] in DIGITS


def isPropKeyAt(chars: str, pos: int, n: int) -> bool:
    """Return True if `chars[pos:]` begins with a KDL1 bare identifier immediately followed by `=`."""
    m = _matchKdl1Ident(chars, pos)
    return m is not None and m.end() < n and chars[m.end()] == "="


def isTagStartAt(chars: str, pos: int, n: int) -> bool:
    """Return True if `pos` starts a `(` tag whose ident begins with `#` or a KDL1 dot-digit ident."""
    if pos + 1 >= n or chars[pos] != "(":
        return False
    nextCh = chars[pos + 1]
    return nextCh == "#" or startsWithDotIdent(chars, pos + 1, n)


def isHashKeywordPropAt(chars: str, pos: int, n: int) -> bool:
    """Return True if `pos` is the `=` right after a KDL2 `#keyword` token used as a KDL1 property key."""
    if pos >= n or chars[pos] != "=":
        return False
    for kw in _KDL1_HASH_KEYWORDS:
        kwLen = len(kw)
        if pos >= kwLen and chars.startswith(kw, pos - kwLen):
            before = pos - kwLen - 1
            if (
                before < 0
                or _matchKdl2IdentChar(chars[before]) is None
                or (before >= 1 and chars[before] == "-" and chars[before - 1] == "/")
            ):
                return True
    return False


def looksLikeAfterNode(chars: str, pos: int, n: int) -> bool:
    """Return True if a KDL2 failure after a node/entry at `pos` looks like a KDL1 construct."""
    if pos >= n:
        return False
    ch = chars[pos]
    if ch == "\ufeff":
        return True
    if isRawStringAt(chars, pos, n):
        return True
    if ch == "#":
        return (pos > 0 and _matchKdl2IdentChar(chars[pos - 1]) is not None) or isPropKeyAt(chars, pos, n)
    if ch == "=":
        return isHashKeywordPropAt(chars, pos, n)
    if ch == "(":
        return isTagStartAt(chars, pos, n)
    if ch == "/":
        j = _afterSlashdash(chars, pos, n)
        if 0 <= j < n:
            ch2 = chars[j]
            if ch2 == "\ufeff":
                return True
            if ch2 == "#":
                return isPropKeyAt(chars, j, n)
            if ch2 == "(":
                return isTagStartAt(chars, j, n)
            return startsWithDotIdent(chars, j, n) and isPropKeyAt(chars, j, n)
        return False
    return startsWithDotIdent(chars, pos, n) and isPropKeyAt(chars, pos, n)


def looksLikeNodeStart(chars: str, pos: int, n: int) -> bool:
    """Return True if unexpected content at node-start `pos` matches a KDL1 node name, tag, or BOM."""
    if pos >= n:
        return False
    ch = chars[pos]
    if ch == "/":
        j = _afterSlashdash(chars, pos, n)
        if j < 0 or j >= n:
            return False
        ch = chars[j]
        pos = j
    return ch in ("\ufeff", "#") or isTagStartAt(chars, pos, n) or startsWithDotIdent(chars, pos, n)


def looksLikeInTag(chars: str, pos: int, n: int) -> bool:
    """Return True if a KDL2 error inside a `(...)` tag at `pos` matches a KDL1 raw string, `#` ident, or BOM."""
    if pos >= n:
        return False
    ch = chars[pos]
    if ch == "\ufeff":
        return True
    if isRawStringAt(chars, pos, n):
        return True
    return ch == "#" and pos > 0 and _matchKdl2IdentChar(chars[pos - 1]) is not None
