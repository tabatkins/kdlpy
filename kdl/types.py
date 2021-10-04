from __future__ import annotations
from collections import OrderedDict
from typing import Optional, NamedTuple, Union
from dataclasses import dataclass
import dataclasses


@dataclass
class Document:
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(self):
        s = ""
        for node in self.children:
            s += node.print()
        return s


@dataclass
class Node:
    name: str
    tag: Optional[str] = None
    entities: list[Entity] = dataclasses.field(default_factory=list)
    children: list[Node] = dataclasses.field(default_factory=list)

    def print(self, indent=0):
        s = "    " * indent
        if self.tag:
            s += f"({self.tag})"
        s += self.name
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


class Entity(NamedTuple):
    key: Optional[str]
    tag: Optional[str]
    value: Union[Binary, Octal, Decimal, Hex]


@dataclass
class Binary:
    value: int

    def print(self):
        return str(self.value)


@dataclass
class Octal:
    value: int

    def print(self):
        return str(self.value)


@dataclass
class Decimal:
    value: Union[int, float]

    def print(self):
        return str(self.value)


@dataclass
class Hex:
    value: int

    def print(self):
        return str(self.value)
        return s
