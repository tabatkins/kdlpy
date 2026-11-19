from __future__ import annotations

from . import t


class ParseError(Exception):
    def __init__(self, s: t.Stream, i: int, details: str) -> None:
        self.line, self.col = s.loc(i)
        loc = f"{self.line}:{self.col}"
        self.msg = f"{loc} {details}"
        if len(self.msg) > 78:
            self.msg = f"{loc}\n  {details}"
        super().__init__(self.msg)


# Public API object passed as the second argument to user-provided `valueConverters` and `nodeConverters`
# (giving them `.fragment` and `.error(msg)`). Keep this class for backwards compatibility, but do not
# use it internally (pass `(s, start, end)` instead to avoid slicing and allocating on valid parses).
class ParseFragment:
    def __init__(self, fragment: str, s: t.Stream, i: int) -> None:
        self.fragment = fragment
        self._s = s
        self._i = i

    def error(self, msg: str) -> ParseError:
        return ParseError(self._s, self._i, msg)
