from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple, Dict, Optional, Callable, Union

from . import parsefuncs
from . import types
from . import printing


class Parser:
    def __init__(
        self,
        parseConfig: Optional[ParseConfig] = None,
        printConfig: Optional[printing.PrintConfig] = None,
    ):
        self.parseConfig = parseConfig
        self.printConfig = printConfig

    def parse(self, chars: str, config: Optional[ParseConfig] = None) -> types.Document:
        doc = parsefuncs.parse(chars, config or self.parseConfig or defaults)
        doc.printConfig = self.printConfig
        return doc


@dataclass
class ParseConfig:
    tags: Dict[str, Callable] = field(default_factory=dict)
    nativeUntaggedValues: bool = True
    nativeTaggedValues: bool = True


defaults = ParseConfig()
