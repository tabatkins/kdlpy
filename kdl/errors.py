from __future__ import annotations

from . import t


class ParseError(Exception):
    """Syntax or value-conversion error carrying a 1-indexed line/column location."""

    def __init__(self, s: t.Stream, i: int, details: str, *, maybeKdl1: bool = False) -> None:
        """Format a parse error at offset `i` in `s`, wrapping long messages onto two lines."""
        self.line, self.col = s.loc(i)
        self.maybeKdl1 = maybeKdl1
        loc = f"{self.line}:{self.col}"
        self.msg = f"{loc} {details}"
        if len(self.msg) > 78:
            self.msg = f"{loc}\n  {details}"
        super().__init__(self.msg)


# Public API object passed as the second argument to user-provided `valueConverters` and `nodeConverters`
# (giving them `.fragment` and `.error(msg)`). Keep this class for backwards compatibility, but do not
# use it internally (pass `(s, start, end)` instead to avoid slicing and allocating on valid parses).
class ParseFragment:
    """Source slice passed to custom value/node converters with an `.error()` helper."""

    __slots__ = ("_i", "_s", "fragment")

    def __init__(self, fragment: str, s: t.Stream, i: int) -> None:
        """Store the matched source text along with its stream and starting offset."""
        self.fragment = fragment
        self._s = s
        self._i = i

    def error(self, msg: str) -> ParseError:
        """Build a `ParseError` anchored at this fragment's starting position."""
        return ParseError(self._s, self._i, msg)
