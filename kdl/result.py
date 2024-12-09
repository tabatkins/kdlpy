from __future__ import annotations

from dataclasses import dataclass

from . import t

if t.TYPE_CHECKING:
    from .stream import Stream


class Failure:
    pass


@dataclass
class ParseFailure(Failure):
    details: str
    s: Stream
    index: int

    def __str__(self) -> str:
        return f"{self.s.loc(self.index)} {self.details}"


ResultT_co = t.TypeVar("ResultT_co", covariant=True)


@dataclass
class Result(t.Generic[ResultT_co]):
    value: ResultT_co | None
    i: int
    err: Failure | None = None

    @property
    def valid(self) -> bool:
        return self.err is None

    @staticmethod
    def fail(index: int) -> Result[ResultT_co]:
        return Result(None, index, Failure())

    @staticmethod
    def parseerror(s: Stream, index: int, details: str) -> Result[ResultT_co]:
        return Result(None, index, ParseFailure(details, s, index))

    @property
    def vi(self) -> tuple[ResultT_co | None, int]:
        # Returns a tuple of the value and index for easy
        # destructuring.
        # If error, value is None for simple detection;
        # use .vie if None is a valid value.
        if self.err:
            value = None
        else:
            value = self.value
        return (value, self.i)

    @property
    def vie(self) -> tuple[ResultT_co | None, int, Failure | None]:
        # Like .vi, but returns the error as the third tuple item.
        if self.err:
            value = None
        else:
            value = self.value
        return (value, self.i, self.err)
