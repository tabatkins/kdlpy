from __future__ import annotations

from dataclasses import dataclass, field

from . import parsefuncs, t

if t.TYPE_CHECKING:
    from .errors import ParseFragment
    from .types import Node, Value

    type ValueConverterT = t.Callable[[Value, ParseFragment], t.Any]
    type NodeConverterT = t.Callable[[Node, ParseFragment], t.Any]


class Parser:
    """Reusable KDL parser bundling default `ParseConfig` and `PrintConfig` settings."""

    __slots__ = ("parseConfig", "printConfig")

    def __init__(
        self,
        parseConfig: ParseConfig | None = None,
        printConfig: t.PrintConfig | None = None,
    ) -> None:
        """Create a parser with optional default parse and print configurations."""
        self.parseConfig = parseConfig
        self.printConfig = printConfig

    def parse(self, chars: str, config: ParseConfig | None = None) -> t.Document:
        """Parse KDL text into a `Document` and attach this parser's default `printConfig`."""
        doc = parsefuncs.parse(chars, config or self.parseConfig or defaults)
        doc.printConfig = self.printConfig
        return doc


@dataclass(slots=True)
class ParseConfig:
    """Options controlling KDL version detection, native value conversion, and custom converters."""

    nativeUntaggedValues: bool = True
    nativeTaggedValues: bool = True
    valueConverters: dict[t.ValueKey, ValueConverterT] = field(default_factory=dict)
    nodeConverters: dict[t.NodeKey, NodeConverterT] = field(
        default_factory=dict,
    )
    version: t.ParseVersion = "auto"


defaults = ParseConfig()
