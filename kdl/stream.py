from dataclasses import dataclass


@dataclass
class Stream:
    _chars: str

    def __getitem__(self, key) -> str:
        try:
            return self._chars[key]
        except IndexError:
            return ""

    def eof(self, index: int) -> bool:
        return index >= len(self._chars)
