from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional, Union, MutableSequence, overload, Iterable, TypeVar
from dataclasses import dataclass
import dataclasses

from .stream import Stream
from .result import Result

NodeT = TypeVar("NodeT", bound="Node")


@dataclass
class Children(MutableSequence[NodeT]):
    _nodes: list[Node] = dataclasses.field(default_factory=list)
    escaped: bool = False

    @overload
    def __getitem__(self, index: int) -> NodeT:
        ...

    @overload
    def __getitem__(self, index: slice) -> Children[NodeT]:
        ...

    def __getitem__(self, index):
        return self._nodes[index]

    @overload
    def __setitem__(self, index: int, item: NodeT) -> None:
        ...

    @overload
    def __setitem__(self, index: slice, item: Iterable[NodeT]) -> None:
        ...

    def __setitem__(self, index, item):
        self._nodes[index] = item

    def __delitem__(self, index: Union[int, slice]) -> None:
        del self._nodes[index]

    def __len__(self) -> int:
        return len(self._nodes)

    def insert(self, index: int, item: NodeT) -> None:
        self._nodes.insert(index, item)


@dataclass
class Document:
    children: Children = dataclasses.field(default_factory=Children)

    def print(self) -> str:
        s = ""
        for node in self.children:
            s += node.print()
        if s == "":
            # always end a kdl doc with a newline
            s = "\n"
        return s


@dataclass
class Node:
    name: str
    tag: Optional[str] = None
    entities: list[Entity] = dataclasses.field(default_factory=list)
    children: Children = dataclasses.field(default_factory=Children)
    escaped: bool = False

    def print(self, indent: int = 0) -> str:
        s = "    " * indent

        if self.tag is not None:
            s += f"({printIdent(self.tag)})"

        s += printIdent(self.name)

        # Print all the values, then all the properties
        # in alpha order, using only the last if a key
        # is duplicated.
        properties = {}
        for entity in self.entities:
            if entity.key is None:
                s += f" {entity.print()}"
            else:
                properties[entity.key] = entity
        for key, entity in sorted(properties.items()):
            s += f" {entity.print()}"

        if self.children:
            s += " {\n"
            for child in self.children:
                s += child.print(indent=indent + 1)
            s += "    " * indent + "}\n"
        else:
            s += "\n"
        return s


@dataclass
class Entity:
    key: Optional[str]
    tag: Optional[str]
    value: Union[Binary, Octal, Decimal, Hex, Keyword, RawString, EscapedString]
    escaped: bool = False

    def print(self):
        if self.key is None:
            s = ""
        else:
            s = printIdent(self.key) + "="
        if self.tag is not None:
            s += f"({printIdent(self.tag)})"
        s += self.value.print()
        return s


@dataclass
class Binary:
    value: int

    def print(self) -> str:
        return str(self.value)


@dataclass
class Octal:
    value: int

    def print(self) -> str:
        return str(self.value)


@dataclass
class Decimal:
    value: Union[int, float]

    def print(self) -> str:
        value = str(self.value)
        if "e" in value:
            value = value.replace("e", "E")
            match = re.match(r"(\d+)(E.*)", value)
            if match:
                value = match.group(1) + ".0" + match.group(2)
        return value


@dataclass
class Hex:
    value: int

    def print(self) -> str:
        return str(self.value)


@dataclass
class Keyword:
    value: str

    def print(self) -> str:
        return self.value


@dataclass
class RawString:
    value: str

    def print(self) -> str:
        return f'"{escapedFromRaw(self.value)}"'


@dataclass
class EscapedString:
    value: str

    def print(self) -> str:
        return f'"{escapedFromRaw(self.value)}"'


def escapedFromRaw(chars: str) -> str:
    return (
        chars.replace("\\", "\\\\")
        .replace('"', '\\"')
        # don't escape a forward slash when printing
        .replace("\b", "\\b")
        .replace("\f", "\\f")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def printIdent(chars):
    if isBareIdent(chars):
        return chars
    return f'"{escapedFromRaw(chars)}"'


def isBareIdent(chars: str) -> bool:
    s = Stream(chars)
    _, i = parseBareIdent(s, 0)
    return i != 0 and s.eof(i)


def parseBareIdent(s: Stream, start: int) -> Result:
    res, i = parseIdentStart(s, start)
    if not res:
        return Result.fail(start)
    while isIdentChar(s[i]):
        i += 1
    ident = s[start:i]
    if isKeyword(ident):
        return Result.fail(start)
    return Result(ident, i)


def parseIdentStart(s: Stream, start: int) -> Result:
    if s[start] in "0123456789" or (s[start] in "+-" and s[start + 1] in "0123456789"):
        return Result.fail(start)
    if not isIdentChar(s[start]):
        return Result.fail(start)
    return Result(s[start], start + 1)


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


def isKeyword(ident: str) -> bool:
    return ident in ("null", "true", "false")
