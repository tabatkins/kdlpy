from __future__ import annotations

from . import t


class ParseError(Exception):
    def __init__(self, s: t.Stream, i: int, details: str) -> None:

        self.msg = f"{s.loc(i)} {details}"
        if len(self.msg) > 78:
            self.msg = f"{s.loc(i)}\n  {details}"
        super().__init__(self.msg)


class ParseFragment:
    def __init__(self, fragment: str, s: t.Stream, i: int) -> None:
        self.fragment = fragment
        self._s = s
        self._i = i

    def error(self, msg: str) -> ParseError:
        return ParseError(self._s, self._i, msg)
