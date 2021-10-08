from __future__ import annotations
from typing import Tuple, Union

from . import stream


class ParseError(Exception):
    def __init__(self, s: Union[str, stream.Stream], i: int, msg: str):
        self.line, self.col = lineCol(s, i)
        self.msg = f"{self.line}:{self.col} parse error:"
        if "\n" in msg:
            self.msg += "\n" + msg
        elif len(self.msg) + len(msg) + 1 > 78:
            self.msg += "\n  " + msg
        else:
            self.msg += " " + msg
        super().__init__(self.msg)


def lineCol(s: Union[str, stream.Stream], index: int) -> Tuple[int, int]:
    """Determines the line and column from an index."""
    line = 1
    col = 1
    for i in range(index):
        if s[i] == "\n":
            line += 1
            col = 1
            continue
        col += 1
    return line, col


class ParseFragment:
    def __init__(self, fragment: str, s: stream.Stream, i: int):
        self.fragment = fragment
        self._s = s
        self._i = i

    def error(self, msg: str) -> ParseError:
        return ParseError(self._s, self._i, msg)
