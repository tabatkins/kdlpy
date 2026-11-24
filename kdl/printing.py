from __future__ import annotations

from dataclasses import dataclass

from . import t


@dataclass(slots=True)
class PrintConfig:
    """Options controlling indentation, string/radix formatting, null emission, and property sorting."""

    indent: str = "\t"
    semicolons: bool = False
    printNullArgs: bool = True
    printNullProps: bool = True
    respectRadix: bool = True
    respectStringType: t.StringTypeMode = "auto"
    exponent: str = "e"
    sortProperties: bool = False


defaults = PrintConfig()
