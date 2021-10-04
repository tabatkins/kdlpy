from __future__ import annotations
from collections import OrderedDict
from typing import Optional


class Document:
    def __init__(self, nodes: Optional[list[Node]] = None):
        if nodes is None:
            nodes = []
        self.nodes = nodes

    def print(self):
        s = ""
        for node in self.nodes:
            s += node.print()
        return s


class Node:
    def __init__(
        self, name: str, tag: str = None, args=None, props=None, children=None
    ):
        self.name = name
        self.tag = tag
        self.args = args if args is not None else []
        self.props = props if props is not None else OrderedDict()
        self.children = children if children is not None else []

    def print(self, indent=0):
        s = "    " * indent
        if self.tag:
            s += f"({self.tag}){self.name}"
        else:
            s += self.name
        for arg in self.args:
            s += f" {arg.print()}"
        for key, val in self.props.items():
            s += f" {key}={val.print()}"
        if self.children:
            s += " {\n"
            for child in self.children:
                s += child.print(indent=indent + 1)
            s += "    " * indent + "}\n"
        else:
            s += "\n"
        return s


class Number:
    def __init__(self, value, tag=None):
        self.value = value
        self.tag = tag

    def print(self):
        s = f"({self.tag})" if self.tag else ""
        s += str(self.value)
        return s
