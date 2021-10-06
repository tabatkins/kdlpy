from dataclasses import dataclass
from typing import Union

@dataclass
class PrintConfig:
	indent: str = "\t"
	semicolons: bool = False
	printNullArgs: bool = True
	printNullProps: bool = True
	respectRadix: bool = True
	exponent: str = "e"

defaultPrintConfig = PrintConfig()