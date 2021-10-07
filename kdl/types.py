from __future__ import annotations

import re
from collections import OrderedDict
from typing import Any, Optional, Union, MutableSequence, overload, Iterable, TypeVar
from dataclasses import dataclass
import dataclasses
from abc import ABC

from . import printing

KDLValue = Union[
    str,
    int,
    float,
    bool,
    None,
    "Value",
]


@dataclass
class Document:
    children: list[Node] = dataclasses.field(default_factory=list)
    printConfig: Optional[printing.PrintConfig] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        config = config or self.printConfig or printing.defaults
        s = ""
        for node in self.children:
            node = toKdlNode(node)
            s += node.print(0, config)
        if s == "":
            # always end a kdl doc with a newline
            s = "\n"
        return s

    def __str__(self) -> str:
        return self.print()


class Component(ABC):
    pass


@dataclass
class Node(Component):
    name: str
    tag: Optional[str] = None
    values: list[Any] = dataclasses.field(default_factory=list)
    properties: OrderedDict[str, Any] = dataclasses.field(default_factory=OrderedDict)
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(
        self, indentLevel: int = 0, config: Optional[printing.PrintConfig] = None
    ) -> str:
        if config is None:
            config = printing.defaults

        s = config.indent * indentLevel

        if self.tag is not None:
            s += f"({printIdent(self.tag)})"

        s += printIdent(self.name)

        # Print all the values, then all the properties
        # in alpha order, using only the last if a key
        # is duplicated.
        for value in self.values:
            value = toKdlValue(value)
            if not config.printNullArgs and value.value is None:
                continue
            s += f" {printValue(value, config)}"

        for key, value in sorted(self.properties.items(), key=lambda x: x[0]):
            value = toKdlValue(value)
            if not config.printNullProps and value.value is None:
                continue
            s += f" {printIdent(key)}={printValue(value, config)}"

        if self.children:
            childrenText = ""
            for child in self.children:
                child = toKdlNode(child)
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

    def __str__(self) -> str:
        return self.print()


class Value(Component):
    pass


@dataclass
class Binary(Value):
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        s = printTag(self.tag)
        if config.respectRadix:
            s += bin(self.value)
        else:
            s += str(self.value)
        return s

    def __str__(self) -> str:
        return self.print()


@dataclass
class Octal(Value):
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        s = printTag(self.tag)
        if config.respectRadix:
            s += oct(self.value)
        else:
            s += str(self.value)
        return s

    def __str__(self) -> str:
        return self.print()


@dataclass
class Decimal(Value):
    mantissa: Union[int, float]
    exponent: int = 0
    tag: Optional[str] = None

    @property
    def value(self) -> float:
        return self.mantissa * (10.0 ** self.exponent)

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        s = printTag(self.tag) + str(self.mantissa)
        if self.exponent != 0:
            s += config.exponent
            if self.exponent > 0:
                s += "+"
            s += str(self.exponent)
        return s

    def __str__(self) -> str:
        return self.print()


@dataclass
class Hex(Value):
    value: int
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        s = printTag(self.tag)
        if config.respectRadix:
            s += hex(self.value)
        else:
            s += str(self.value)
        return s

    def __str__(self) -> str:
        return self.print()


@dataclass
class Bool(Value):
    value: bool
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        return printTag(self.tag) + ("true" if self.value else "false")

    def __str__(self) -> str:
        return self.print()


@dataclass
class Null(Value):
    tag: Optional[str] = None

    @property
    def value(self) -> None:
        return None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        return printTag(self.tag) + "null"

    def __str__(self) -> str:
        return self.print()


@dataclass
class RawString(Value):
    value: str
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        if config.respectStringType:
            hashes = "#" * findRequiredHashCount(self.value)
            return f'{printTag(self.tag)}r{hashes}"{self.value}"{hashes}'
        else:
            return f'{printTag(self.tag)}"{escapedFromRaw(self.value)}"'

    def __str__(self) -> str:
        return self.print()


def findRequiredHashCount(chars: str) -> int:
    for i in range(0, 100):
        ender = '"' + ("#" * i)
        if ender not in chars:
            return i
    assert False, "A raw string requires more than 100 hashes???"


@dataclass
class String(Value):
    value: str
    tag: Optional[str] = None

    def print(self, config: Optional[printing.PrintConfig] = None) -> str:
        if config is None:
            config = printing.defaults
        return f'{printTag(self.tag)}"{escapedFromRaw(self.value)}"'

    def __str__(self) -> str:
        return self.print()


def toKdlNode(val: Any) -> Node:
    if isinstance(val, Node):
        return val
    if not callable(getattr(val, "to_kdl", None)):
        raise Exception(
            f"Can't convert object to KDL for serialization. Got:\n{repr(val)}"
        )
    node = val.to_kdl()
    if not isinstance(node, Node):
        raise Exception(f"Expected object to convert to KDL Node. Got:\n{repr(val)}")
    return node


def toKdlValue(val: Any) -> KDLValue:
    if isKdlValue(val):
        return val
    if not callable(getattr(val, "to_kdl", None)):
        raise Exception(
            f"Can't convert object to KDL for serialization. Got:\n{repr(val)}"
        )
    value = val.to_kdl()
    if not isKdlValue(value):
        raise Exception(
            f"Expected object to convert to KDL value or compatible primitive. Got:\n{repr(val)}"
        )
    return value


def isKdlValue(val: Any) -> bool:
    if val is None:
        return True
    return isinstance(
        val,
        (
            str,
            int,
            float,
            bool,
            Value,
        ),
    )


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


def printValue(val: Any, config: printing.PrintConfig) -> str:
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return f'"{escapedFromRaw(val)}"'
    if val is True:
        return "true"
    if val is False:
        return "false"
    if val is None:
        return "null"
    return val.print(config)


def printIdent(chars: str) -> str:
    if isBareIdent(chars):
        return chars
    return f'"{escapedFromRaw(chars)}"'


def isBareIdent(chars: str) -> bool:
    if not chars:
        return False
    if any(not isIdentChar(x) for x in chars):
        return False
    if chars[0] in "0123456789":
        return False
    if len(chars) > 1 and chars[0] in "+-" and chars[1] in "0123456789":
        return False
    if chars in ("true", "false", "null"):
        return False
    return True


def isIdentChar(ch: str) -> bool:
    if not ch:
        return False
    # reserved characters
    if ch in r'''\/(){}<>;[]=,"''':
        return False
    cp = ord(ch)
    # nonprintable
    if cp < 0x20:
        return False
    # invalid codepoint
    if cp > 0x10FFFF:
        return False
    # spaces
    if cp in (0x09, 0x20, 0xA0, 0x1680, 0x202F, 0x205F, 0x3000, 0xFEFF):
        return False
    # more spaces
    if 0x2000 <= cp <= 0x200A:
        return False
    # line breaks
    if cp in (0x0A, 0x0D, 0x85, 0x0C, 0x2028, 0x2029):
        return False
    return True
