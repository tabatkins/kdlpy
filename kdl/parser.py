import collections
import typing
from typing import NamedTuple, Any, Optional

from . import types
from .errors import ParseError


def parse(input):
    return parseDocument(Stream(input), 0)


def parseDocument(s, start):
    nodes = []
    _, i = parseLinespace(s, start)
    while True:
        if i >= len(s):
            return nodes
        node, i = parseNode(s, i)
        nodes.append(node)
        _, i = parseLinespace(s, i)


def parseNode(s, start):
    i = start
    # tag?
    tag, i = parseTag(s, i, throw=True)

    # name
    name, i = parseIdent(s, i, throw=True)

    _, i = parseLinespace(s, i)
    _, i = parseNodeTerminator(s, i)

    return Result(types.Node(name=name, tag=tag), i)


def parseTag(s, start, throw=False):
    if s[start] != "(":
        return Result.fail(start)
    tag, end = parseIdent(s, start + 1, throw=throw)
    if tag is None:
        return Result.fail(start)
    if s[end] != ")":
        if throw:
            raise ParseError(s,end,f"Junk between tag ident and closing paren.")
        return Result.fail(start)
    return Result(tag, end + 1)


def parseIdent(s, start, throw=False):
    res, i = parseIdentStart(s, start, throw=throw)
    if not res:
        return Result.fail(start)
    while isIdentChar(s[i]):
        i += 1
    return Result(s[start:i], i)


def parseIdentStart(s, i, throw=False):
    if s[i] in "0123456789" or (s[i] in "+-" and s[i+1] in "0123456789"):
        if throw:
            raise ParseError(s,i,f"Idents must not be confusable with numbers.")
        return Result.fail(i)
    if not isIdentChar(s[i]):
        if throw:
            raise ParseError(s,i,f"Idents must start with an identifier character.")
        return Result.fail(i)
    return Result(s[i], i+1)

def parseNodeTerminator(s, start):
    res = parseNewline(s, start)
    if res.valid:
        return res
    if s[start] == ";":
        return Result(";", start+1)
    if start >= len(s):
        return Result(True, start)
    return Result.fail(start)

def parseNewline(s, start):
    if s[start] == "\x0d" and s[start+1] == "\x0a":
        return Result("\n", start+2)
    if isNewlineChar(s[start]):
        return Result("\n", start+1)
    return Result.fail(start)

def parseLinespace(s, start):
    if not isLinespaceChar(s[start]):
        return Result.fail(start)
    end = start + 1
    while isLinespaceChar(s[end]):
        end += 1
    return Result(s[start:end], end)


def isIdentChar(ch):
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


def isWSChar(ch):
    if not ch:
        return False
    cp = ord(ch)
    if cp in (0x09, 0x20, 0xA0, 0x1680, 0x202F, 0x205F, 0x3000, 0xFEFF):
        return True
    if 0x2000 <= cp <= 0x200A:
        return True
    return False


def isNewlineChar(ch):
    if not ch:
        return False
    cp = ord(ch)
    return cp in (0x0A, 0x0D, 0x85, 0x0C, 0x2028, 0x2029)


def isLinespaceChar(ch):
    if not ch:
        return False
    return isWSChar(ch) or isNewlineChar(ch)


class Stream:
    def __init__(self, chars):
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
    index: int

    @property
    def valid(self):
        return bool(self.value)

    @staticmethod
    def fail(index):
        return Result(None, index)

