from __future__ import annotations
import collections
import typing
from typing import NamedTuple, Any, Optional

from . import types
from .errors import ParseError


def parse(input):
    return parseDocument(Stream(input), 0)


def parseDocument(s: Stream, start: int = 0) -> types.Document:
    doc = types.Document()
    _, i = parseLinespace(s, start)
    while True:
        if i >= len(s):
            return doc
        node, i = parseNode(s, i)
        doc.nodes.append(node)
        _, i = parseLinespace(s, i)


def parseNode(s: Stream, start: int) -> Result:
    i = start
    # tag?
    tag, i = parseTag(s, i, throw=True)

    # name
    name, i = parseIdent(s, i, throw=True)

    _, i = parseNodespace(s, i)
    _, i = parseNodeTerminator(s, i, throw=True)

    return Result(types.Node(name=name, tag=tag), i)


def parseTag(s: Stream, start: int, throw: bool = False) -> Result:
    if s[start] != "(":
        return Result.fail(start)
    tag, end = parseIdent(s, start + 1, throw=throw)
    if tag is None:
        return Result.fail(start)
    if s[end] != ")":
        if throw:
            raise ParseError(s, end, f"Junk between tag ident and closing paren.")
        return Result.fail(start)
    return Result(tag, end + 1)


def parseIdent(s: Stream, start: int, throw: bool = False) -> Result:
    res, i = parseIdentStart(s, start, throw=throw)
    if not res:
        return Result.fail(start)
    while isIdentChar(s[i]):
        i += 1
    ident = s[start:i]
    if isKeyword(ident):
        if throw:
            raise ParseError(
                s,
                start,
                f"Expected a keyword, but got a reserved identifier '{ident}'.",
            )
        return Result.fail(start)
    return Result(ident, i)


def parseIdentStart(s: Stream, start: int, throw: bool = False) -> Result:
    if s[start] in "0123456789" or (s[start] in "+-" and s[start + 1] in "0123456789"):
        if throw:
            raise ParseError(s, start, f"Idents must not be confusable with numbers.")
        return Result.fail(start)
    if not isIdentChar(s[start]):
        if throw:
            raise ParseError(
                s, start, f"Idents must start with an identifier character."
            )
        return Result.fail(start)
    return Result(s[start], start + 1)


def parseNodeTerminator(s: Stream, start: int, throw: bool = False) -> Result:
    res = parseNewline(s, start)
    if res.valid:
        return res
    if s[start] == ";":
        return Result(";", start + 1)
    if start >= len(s):
        return Result(True, start)
    if throw:
        raise ParseError(s, start, f"Junk after node, before terminator.")
    return Result.fail(start)


def parseNewline(s: Stream, start: int, throw: bool = False) -> Result:
    if s[start] == "\x0d" and s[start + 1] == "\x0a":
        return Result("\n", start + 2)
    if isNewlineChar(s[start]):
        return Result("\n", start + 1)
    return Result.fail(start)


def parseLinespace(s: Stream, start: int, throw: bool = False) -> Result:
    if not isLinespaceChar(s[start]):
        if throw:
            raise ParseError(s, start, "Expected WS or linebreak.")
        return Result.fail(start)
    end = start + 1
    while isLinespaceChar(s[end]):
        end += 1
    return Result(s[start:end], end)


def parseNodespace(s: Stream, start: int, throw: bool = False) -> Result:
    return parseWhitespace(s, start, throw=throw)


def parseWhitespace(s: Stream, start: int, throw: bool = False) -> Result:
    if not isWSChar(s[start]):
        if throw:
            raise ParseError(s, start, "Expected WS.")
        return Result.fail(start)
    end = start + 1
    while isWSChar(s[end]):
        end += 1
    return Result(s[start:end], end)


def isIdentChar(ch: str) -> bool:
    if not ch:
        return False
    if ch in r'''\/(){}<>;[]=,"''':
        return False
    if isLinespaceChar(ch):
        return False
    cp = ord(ch)
    if cp <= 0x20:
        return False
    if cp > 0x10FFFF:
        return False
    return True


def isKeyword(ident: str) -> bool:
    return ident in ("null", "true", "false")


def isWSChar(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    if cp in (0x09, 0x20, 0xA0, 0x1680, 0x202F, 0x205F, 0x3000, 0xFEFF):
        return True
    if 0x2000 <= cp <= 0x200A:
        return True
    return False


def isNewlineChar(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return cp in (0x0A, 0x0D, 0x85, 0x0C, 0x2028, 0x2029)


def isLinespaceChar(ch: str) -> bool:
    if not ch:
        return False
    return isWSChar(ch) or isNewlineChar(ch)


class Stream:
    def __init__(self, chars: str):
        self._chars = chars
        self._len = len(chars)

    def __len__(self):
        return self._len

    def __getitem__(self, key):
        if isinstance(key, int):
            if key < 0 or key >= len(self):
                return ""
            else:
                return self._chars[key]
        return self._chars[key]


class Result(NamedTuple):
    value: Any
    end: int

    @property
    def valid(self) -> bool:
        return self.value is not None

    @staticmethod
    def fail(index: int) -> Result:
        return Result(None, index)
