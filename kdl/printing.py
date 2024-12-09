from dataclasses import dataclass


@dataclass
class PrintConfig:
    indent: str = "\t"
    semicolons: bool = False
    printNulls: bool = True
    respectRadix: bool = True
    respectStringType: bool = True
    exponent: str = "e"
    sortEntries: bool = False


defaults = PrintConfig()
