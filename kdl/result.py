from __future__ import annotations

from typing import NamedTuple, Any


class Result(NamedTuple):
    value: Any
    end: int

    @property
    def valid(self) -> bool:
        return self.value is not None

    @staticmethod
    def fail(index: int) -> Result:
        return Result(None, index)
