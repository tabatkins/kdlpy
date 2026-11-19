from __future__ import annotations

import itertools
import re

# Character class definitions per KDL 2.0 specification
WS_RE = r"\t \xa0\u1680\u2000-\u200a\u202f\u205f\u3000"
NL_RE = r"\n\r\x85\x0b\x0c\u2028\u2029"
DISALLOWED_RE = r"\x00-\x08\x0e-\x1f\x7f\ud800-\udfff\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff"
RESERVED_RE = r"(){}\[\]/\\\"#;="
IDENT_CHAR_RE_STR = rf"[^{RESERVED_RE}{WS_RE}{NL_RE}{DISALLOWED_RE}]"

WS_CHARS = "\t \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"
NL_CHARS = "\n\r\x85\x0b\x0c\u2028\u2029"

NL_SET = frozenset(NL_CHARS)
LINESPACE_CHAR_SET = frozenset(WS_CHARS + NL_CHARS)
DIGITS = frozenset("0123456789")

POS_INF = float("inf")
NEG_INF = float("-inf")
NAN = float("nan")

NATIVE_KEYWORDS: dict[str, bool | float | None] = {
    "true": True,
    "false": False,
    "null": None,
    "inf": POS_INF,
    "-inf": NEG_INF,
    "nan": NAN,
}
KEYWORDS_LOWER = frozenset(NATIVE_KEYWORDS)

# All 88 ASCII case variations of KDL keywords (`true`, `false`, `null`, `inf`, `-inf`, `nan`).
# Precomputed so bare-ident checks are a single O(1) lookup without `.lower()` allocations
# (no non-ASCII codepoint lowercases to any of these strings).
CONFUSABLE_KEYWORDS: frozenset[str] = frozenset(
    "".join(letters)
    for kw in KEYWORDS_LOWER
    for letters in itertools.product(*((ch.lower(), ch.upper()) if ch.isalpha() else (ch,) for ch in kw))
)

BARE_IDENT_PATTERN = re.compile(rf"(?![+-]?\.?[0-9]){IDENT_CHAR_RE_STR}+")
_fullmatchBareIdent = BARE_IDENT_PATTERN.fullmatch


def isBareIdent(chars: str) -> bool:
    # Every ASCII Python identifier (`[A-Za-z_][A-Za-z0-9_]*`) is a valid KDL bare ident (except keywords).
    if chars.isascii() and chars.isidentifier():
        return chars not in CONFUSABLE_KEYWORDS
    if _fullmatchBareIdent(chars) is None:
        return False
    return chars not in CONFUSABLE_KEYWORDS
