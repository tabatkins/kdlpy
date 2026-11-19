from __future__ import annotations

from dataclasses import dataclass

from . import t


@dataclass
class Stream:
    chars: str
    length: int
    startLine: int
    config: t.ParseConfig
    fastUntagged: bool

    def __init__(self, chars: str, config: t.ParseConfig, startLine: int = 1) -> None:
        self.chars = chars
        self.length = len(chars)
        self.startLine = startLine
        self.config = config
        # When true, untagged values can return Python natives directly without allocating Value wrappers.
        self.fastUntagged = not config.valueConverters and config.nativeUntaggedValues

    def __getitem__(self, key: int | slice) -> str:
        if isinstance(key, int):
            if 0 <= key < self.length:
                return self.chars[key]
            return ""
        start = key.start
        stop = key.stop
        if start is not None and start < 0:
            start = 0
        if stop is not None and stop < 0:
            stop = 0
        return self.chars[start : stop : key.step]

    def eof(self, index: int) -> bool:
        return index >= self.length

    def __len__(self) -> int:
        return self.length

    def loc(self, index: int) -> tuple[int, int]:
        # Computed lazily on error via C-level str methods instead of indexing all newlines up front.
        if index <= 0:
            return (self.startLine, index + 1)
        line = self.chars.count("\n", 0, index) + self.startLine
        # rfind returns -1 on line 1, giving `index - (-1) == index + 1` (1-indexed column).
        col = index - self.chars.rfind("\n", 0, index)
        return (line, col)

    def line(self, index: int) -> int:
        return self.loc(index)[0]

    def col(self, index: int) -> int:
        return self.loc(index)[1]
