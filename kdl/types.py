from __future__ import annotations

import re
from collections import OrderedDict
from typing import Optional, Union, MutableSequence, overload, Iterable, TypeVar
from dataclasses import dataclass
import dataclasses

from .stream import Stream
from .result import Result, Failure
from . import printing

NodeT = TypeVar("NodeT", bound="Node")
Entity = Union[
    "Binary", "Octal", "Decimal", "Hex", "Bool", "Null", "RawString", "String"
]


@dataclass
class Document:
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        s = ""
        for node in self.children:
            s += node.print(0, config)
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
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(
        self, indentLevel: int = 0, config: Optional[printing.PrintConfig] = None
    ) -> str:
        if config is None:
            config = printing.defaultPrintConfig

        s = config.indent * indentLevel

        if self.tag is not None:
            s += f"({printIdent(self.tag)})"

        s += printIdent(self.name)

        # Print all the values, then all the properties
        # in alpha order, using only the last if a key
        # is duplicated.
        for value in self.values:
            if (
                not config.printNullArgs
                and isinstance(value, Keyword)
                and value.value == "null"
            ):
                continue
            s += f" {value.print(config)}"

        for key, value in sorted(self.properties.items(), key=lambda x: x[0]):
            if (
                not config.printNullProps
                and isinstance(value, Keyword)
                and value.value == "null"
            ):
                continue
            s += f" {printIdent(key)}={value.print(config)}"

        if self.children:
            childrenText = ""
            for child in self.children:
                childrenText += child.print(indentLevel=indentLevel + 1, config=config)
            if childrenText:
                s += " {\n"
                s += childrenText
                s += config.indent * indentLevel + "}"
        if config.semicolons:
            s += ";\n"
        else:
            s += "\n"
        return s


@dataclass
class Binary:
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        s = printTag(self.tag)
        if config.respectRadix:
            s += "0b" + bin(self.value)
        else:
            s += str(self.value)
        return s


@dataclass
class Octal:
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        s = printTag(self.tag)
        if config.respectRadix:
            s += "0o" + oct(self.value)
        else:
            s += str(self.value)
        return s


@dataclass
class Decimal:
    mantissa: Union[int, float]
    exponent: int = 0
    tag: Optional[str] = None

    @property
    def value(self):
        return self.mantissa * (10.0 ** self.exponent)

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        s = printTag(self.tag) + str(self.mantissa)
        if self.exponent != 0:
            s += config.exponent
            if self.exponent > 0:
                s += "+"
            s += str(self.exponent)
        return s


@dataclass
class Hex:
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        s = printTag(self.tag)
        if config.respectRadix:
            s += "0x" + hex(self.value)
        else:
            s += str(self.value)
        return s


@dataclass
class Bool:
    value: bool
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        return printTag(self.tag) + ("true" if self.value else "false")


@dataclass
class Null:
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        return printTag(self.tag) + "null"


@dataclass
class RawString:
    value: str
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
        if config.respectStringType:
            hashes = "#" * findRequiredHashCount(self.value)
            return f'{printTag(self.tag)}r{hashes}"{self.value}"{hashes}'
        else:
            return f'{printTag(self.tag)}"{escapedFromRaw(self.value)}"'


def findRequiredHashCount(chars: str) -> int:
    for i in range(0, 100):
        ender = '"' + ("#" * i)
        if ender not in chars:
            return i


@dataclass
class String:
    value: str
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaultPrintConfig
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
    if res is Failure:
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
