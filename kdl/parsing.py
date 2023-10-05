from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple, Union

from . import parsefuncs, printing, types


class Parser:
    def __init__(
        self,
        parseConfig: Optional[ParseConfig] = None,
        printConfig: Optional[printing.PrintConfig] = None,
    ) -> None:
        self.parseConfig = parseConfig
        self.printConfig = printConfig

    def parse(self, chars: str, config: Optional[ParseConfig] = None) -> types.Document:
        doc = parsefuncs.parse(chars, config or self.parseConfig or defaults)
        doc.printConfig = self.printConfig
        return doc


@dataclass
class ParseConfig:
    nativeUntaggedValues: bool = True
    nativeTaggedValues: bool = True
    valueConverters: Dict[str, Callable] = field(default_factory=dict)
    nodeConverters: Dict[Union[str, Tuple[str, str]], Callable] = field(
        default_factory=dict,
    )


defaults = ParseConfig()
