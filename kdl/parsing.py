from __future__ import annotations

from dataclasses import dataclass, field

from . import parsefuncs, t


class Parser:
    def __init__(
        self,
        parseConfig: ParseConfig | None = None,
        printConfig: t.PrintConfig | None = None,
    ) -> None:
        self.parseConfig = parseConfig
        self.printConfig = printConfig

    def parse(self, chars: str, config: ParseConfig | None = None) -> t.Document:
        doc = parsefuncs.parse(chars, config or self.parseConfig or defaults)
        doc.printConfig = self.printConfig
        return doc


@dataclass
class ParseConfig:
    nativeUntaggedValues: bool = True
    nativeTaggedValues: bool = True
    valueConverters: dict[str, t.Callable] = field(default_factory=dict)
    nodeConverters: dict[str | tuple[str, str], t.Callable] = field(
        default_factory=dict,
    )


defaults = ParseConfig()
