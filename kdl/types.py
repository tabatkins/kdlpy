from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional, Union
from dataclasses import dataclass
import dataclasses

from .stream import Stream
from .result import Result


@dataclass
class Document:
    children: list[Node] = dataclasses.field(default_factory=list)

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
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(self, indent: int = 0) -> str:
        s = "    " * indent
        if self.tag:
            s += f"({self.tag})"
        if isBareIdent(self.name):
            s += self.name
        else:
            s += f'"{escapedFromRaw(self.name)}"'
        for key, tag, value in self.entities:
            s += " "
            if key is None:
                if tag is not None:
                    s += f"({tag})"
                s += f"{value.print()}"
            else:
                s += key + "="
                if tag is not None:
                    s += f"({tag})"
                s += f"{value.print()}"
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
    value: Union[Binary, Octal, Decimal, Hex]

    def __iter__(self):
        return iter((self.key, self.tag, self.value))


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
    return chars.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


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
