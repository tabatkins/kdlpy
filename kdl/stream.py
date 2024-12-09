from __future__ import annotations

from dataclasses import dataclass
import bisect
import re

from . import t

if t.TYPE_CHECKING:
    from .result import Result


@dataclass
class Stream:
    _chars: str
    _len: int
    _lineBreaks: list[int]
    startLine: int
    config: t.ParseConfig

    def __init__(self, chars: str, config: t.ParseConfig, startLine: int = 1) -> None:
        self._chars = chars
        self._len = len(chars)
        self._lineBreaks = []
        self.startLine = startLine
        self.config = config
        for i, char in enumerate(chars):
            if char == "\n":
                self._lineBreaks.append(i)

    def __getitem__(self, key: int | slice) -> str:
        if isinstance(key, int):
            if key < 0:
                return ""
        else:
            if key.start < 0:
                key = slice(0, key.stop, key.step)
            if key.stop < 0:
                key = slice(key.start, 0, key.step)
        try:
            return self._chars[key]
        except IndexError:
            return ""

    def eof(self, index: int) -> bool:
        return index >= self._len

    def __len__(self) -> int:
        return self._len

    def line(self, index: int) -> int:
        # Zero-based line index
        lineIndex = bisect.bisect_left(self._lineBreaks, index)
        return lineIndex + self.startLine

    def col(self, index: int) -> int:
        lineIndex = bisect.bisect_left(self._lineBreaks, index)
        if lineIndex == 0:
            return index + 1
        startOfCol = self._lineBreaks[lineIndex - 1]
        return index - startOfCol

    def loc(self, index: int) -> str:
        return f"{self.line(index)}:{self.col(index)}"

    def skipTo(self, start: int, text: str) -> Result[str]:
        # Skip forward until encountering `text`.
        # Produces the text encountered before this point.
        i = start
        textLen = len(text)
        while not self.eof(i):
            if self[i : i + textLen] == text:
                break
            i += 1
        if self[i : i + textLen] == text:
            return Result(self[start:i], i)
        else:
            return Result.fail(start)

    def matchRe(self, start: int, pattern: re.Pattern) -> Result[re.Match]:
        match = pattern.match(self._chars, start)
        if match:
            return Result(match, match.end())
        else:
            return Result.fail(start)

    def searchRe(self, start: int, pattern: re.Pattern) -> Result[re.Match]:
        match = pattern.search(self._chars, start)
        if match:
            return Result(match, match.end())
        else:
            return Result.fail(start)
