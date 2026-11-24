from __future__ import annotations

from . import t


class Stream:
    """Input text and parse-config wrapper with lazy 1-indexed line/column computation."""

    __slots__ = ("chars", "config", "fastUntagged", "length", "startLine")

    chars: str
    length: int
    startLine: int
    config: t.ParseConfig
    fastUntagged: bool

    def __init__(self, chars: str, config: t.ParseConfig, startLine: int = 1) -> None:
        """Wrap `chars` and `config`, precomputing whether untagged values bypass `Value` wrappers."""
        self.chars = chars
        self.length = len(chars)
        self.startLine = startLine
        self.config = config
        # When true, untagged values can return Python natives directly without allocating Value wrappers.
        self.fastUntagged = not config.valueConverters and config.nativeUntaggedValues

    def loc(self, index: int) -> tuple[int, int]:
        """Compute the 1-indexed `(line, col)` location for offset `index`."""
        # Computed lazily on error via C-level str methods instead of indexing all newlines up front.
        if index <= 0:
            return (self.startLine, 1)
        chars = self.chars
        if chars.find("\r", 0, index) == -1:
            line = chars.count("\n", 0, index) + self.startLine
            # rfind returns -1 on line 1, giving `index - (-1) == index + 1` (1-indexed column).
            col = index - chars.rfind("\n", 0, index)
            return (line, col)
        # Handle \r\n (single line break) and bare \r as line breaks too.
        line = (
            self.startLine + chars.count("\n", 0, index) + chars.count("\r", 0, index) - chars.count("\r\n", 0, index)
        )
        lastBreak = max(chars.rfind("\n", 0, index), chars.rfind("\r", 0, index))
        return (line, index - lastBreak)
