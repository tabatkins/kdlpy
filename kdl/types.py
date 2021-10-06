from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional, Union, MutableSequence, overload, Iterable, TypeVar
from dataclasses import dataclass
import dataclasses

from .stream import Stream
from .result import Result

NodeT = TypeVar("NodeT", bound="Node")
Entity = Union[
    "Binary", "Octal", "Decimal", "Hex", "Keyword", "RawString", "EscapedString"
]


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
    values: list[Entity] = dataclasses.field(default_factory=list)
    properties: OrderedDict[str, Entity] = dataclasses.field(
        default_factory=OrderedDict
    )
    children: Children = dataclasses.field(default_factory=Children)
    escaped: bool = False

    def print(self, indent: int = 0) -> str:
        if self.escaped:
            return ""

        s = "    " * indent

        if self.tag is not None:
            s += f"({printIdent(self.tag)})"

        s += printIdent(self.name)

        # Print all the values, then all the properties
        # in alpha order, using only the last if a key
        # is duplicated.
        for value in self.values:
            if not value.escaped:
                s += f" {value.print()}"

        for key, val in sorted(self.properties.items(), key=lambda x: x[0]):
            if not val.escaped:
                s += f" {printIdent(key)}={val.print()}"

        if self.children and not self.children.escaped:
            childrenText = ""
            for child in self.children:
                childrenText += child.print(indent=indent + 1)
            if childrenText:
                s += " {\n"
                s += childrenText
                s += "    " * indent + "}\n"
            else:
                s += "\n"
        else:
            s += "\n"
        return s


@dataclass
class Binary:
    value: int
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return printTag(self.tag) + str(self.value)


@dataclass
class Octal:
    value: int
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return printTag(self.tag) + str(self.value)


@dataclass
class Decimal:
    mantissa: Union[int, float]
    exponent: int = 0
    tag: Optional[str] = None
    escaped: bool = False

    @property
    def value(self):
        return self.mantissa * (10.0 ** self.exponent)

    def print(self) -> str:
        s = printTag(self.tag) + str(self.mantissa)
        if self.exponent != 0:
            s += "E"
            if self.exponent > 0:
                s += "+"
            s += str(self.exponent)
        return s


@dataclass
class Hex:
    value: int
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return printTag(self.tag) + str(self.value)


@dataclass
class Keyword:
    value: str
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return printTag(self.tag) + self.value


@dataclass
class RawString:
    value: str
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return f'{printTag(self.tag)}"{escapedFromRaw(self.value)}"'


@dataclass
class EscapedString:
    value: str
    tag: Optional[str] = None
    escaped: bool = False

    def print(self) -> str:
        return f'{printTag(self.tag)}"{escapedFromRaw(self.value)}"'


def printTag(tag: Optional[str]) -> str:
    if tag is not None:
        return f"({printIdent(tag)})"
    else:
        return ""


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
