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
        doc.children.append(node)
        _, i = parseLinespace(s, i)


def parseNode(s: Stream, start: int) -> Result:
    i = start
    # tag?
    tag, i = parseTag(s, i, throw=True)

    # name
    name, i = parseIdent(s, i, throw=True)

    # props and values
    entities = []
    while True:
        space, i = parseNodespace(s, i)
        if space is None:
            break
        prop, i = parseProperty(s, i)
        if prop:
            entities.append(prop)
            continue
        val, i = parseValue(s, i)
        if val:
            entities.append(val)
            continue
        break

    _, i = parseNodespace(s, i)
    _, i = parseNodeTerminator(s, i, throw=True)

    return Result(types.Node(name=name, tag=tag, entities=entities), i)


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
    # Look for string-type idents as well
    return parseBareIdent(s, start, throw=throw)


def parseBareIdent(s: Stream, start: int, throw: bool = False) -> Result:
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


def parseProperty(s: Stream, start: int) -> Result:
    return Result.fail(start)


def parseValue(s: Stream, start: int) -> Result:
    tag, i = parseTag(s, start)

    num, i = parseNumber(s, i)
    if num is not None:
        return Result(types.Entity(None, tag, num), i)

    # Failed to find a value
    # But if I found a tag, something's up
    if tag:
        raise ParseError(s, start, "Found a tag, but no value following it.")
    return Result.fail(start)


def parseNumber(s: Stream, start: int, throw: bool = False) -> Result:
    if not isNumberStart(s, start):
        return Result.fail(start)
    res = parseBinaryNumber(s, start, throw=throw)
    if res.valid:
        return res
    res = parseOctalNumber(s, start, throw=throw)
    if res.valid:
        return res
    res = parseHexNumber(s, start, throw=throw)
    if res.valid:
        return res
    res = parseDecimalNumber(s, start, throw=throw)
    if res.valid:
        return res
    if throw:
        raise ParseError(
            s, start, f"Expected a number, but got junk after the initial digit."
        )
    return Result.fail(start)


def parseBinaryNumber(s: Stream, start: int, throw: bool = False):
    i = start

    # optional sign
    sign = "+"
    if s[i] in "+-":
        sign = s[i]
        i += 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "b"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isBinaryDigit(s[i]):
        if throw:
            raise ParseError(s, i, f"Expected binary digit after 0b, got junk.")
        return Result.fail(start)

    # following digits/underscores
    end = i + 1
    while isBinaryDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 2)
    return Result(types.Binary(value), end)


def parseOctalNumber(s: Stream, start: int, throw: bool = False):
    i = start

    # optional sign
    sign = "+"
    if s[i] in "+-":
        sign = s[i]
        i += 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "o"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isOctalDigit(s[i]):
        if throw:
            raise ParseError(s, i, f"Expected octal digit after 0o, got junk.")
        return Result.fail(start)

    # following digits/underscores
    end = i + 1
    while isOctalDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 8)
    return Result(types.Octal(value), end)


def parseHexNumber(s: Stream, start: int, throw: bool = False):
    i = start

    # optional sign
    sign = "+"
    if s[i] in "+-":
        sign = s[i]
        i += 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "x"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isHexDigit(s[i]):
        if throw:
            raise ParseError(s, i, f"Expected hex digit after 0x, got junk.")
        return Result.fail(start)

    # following digits/underscores
    end = i + 1
    while isHexDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 16)
    return Result(types.Hex(value), end)


def parseDecimalNumber(s: Stream, start: int, throw) -> Result:
    return Result.fail(start)


def isNumberStart(s: Stream, start: int) -> bool:
    # all numbers start with an optional sign
    # followed by a digit:
    # either the first digit of the number,
    # or the 0 of the prefix)
    if isDigit(s[start]):
        return True
    if isSign(s[start]) and isDigit(s[start + 1]):
        return True
    return False


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


def isSign(ch: str) -> bool:
    return ch != "" and ch in "+-"


def isDigit(ch: str) -> bool:
    return ch != "" and ch in "0123456789"


def isBinaryDigit(ch: str) -> bool:
    return ch != "" and ch in "01"


def isOctalDigit(ch: str) -> bool:
    return ch != "" and ch in "01234567"


def isHexDigit(ch: str) -> bool:
    return ch != "" and ch in "0123456789abcdefABCDEF"


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
        try:
            return self._chars[key]
        except IndexError:
            return ""


class Result(NamedTuple):
    value: Any
    end: int

    @property
    def valid(self) -> bool:
        return self.value is not None

    @staticmethod
    def fail(index: int) -> Result:
        return Result(None, index)
