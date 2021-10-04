import collections

from . import types
from . import errors


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
    tag, i = parseTag(s, i)

    # name
    name, i = parseIdent(s, i)
    if not name:
        raise errors.ParseError(s, i, "Couldn't find a valid node name.")

    return Result(types.Node(name=name, tag=tag), i)


def parseTag(s, start):
    if s[start] != "(":
        return Result.fail(start)
    tag, end = parseIdent(s, start + 1)
    if tag is None:
        return Result.fail(start)
    if s[end] != ")":
        return Result.fail(start)
    return Result(tag, end + 1)


def parseIdent(s, start):
    if not isIdentStart(s, start):
        return Result.fail(start)
    end = start + 1
    while isIdentChar(s[end]):
        end += 1
    return Result(s[start:end], end)


def parseLinespace(s, start):
    if not isLinespaceChar(s[start]):
        return Result.fail(start)
    end = start + 1
    while isLinespaceChar(s[end]):
        end += 1
    return Result(s[start:end], end)


def isIdentStart(s, i):
    if s[i] not in "+-0123456789" and isIdentChar(s[i]):
        return True
    if s[i] in "+-":
        return s[i + 1] not in "0123456789" and isIdentChar(s[i + 1])


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


class Result(collections.namedtuple("Result", ["value", "index"])):
    __slots__ = ()

    @property
    def valid(self):
        return bool(self.value)

    @staticmethod
    def fail(index):
        return Result(None, index)
