from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import parsing


@dataclass
class Stream:
    _chars: str
    config: parsing.ParseConfig

    def __getitem__(self, key) -> str:
        try:
            return self._chars[key]
        except IndexError:
            return ""

    def eof(self, index: int) -> bool:
        return index >= len(self._chars)
