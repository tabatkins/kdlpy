# pylint: skip-file
# ruff: noqa
# Module for holding types, for easy importing into the rest of the codebase
from __future__ import annotations

from typing import TYPE_CHECKING, cast, overload, override

from .unset import UNSET as UNSET

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    import datetime
    import decimal
    import ipaddress
    import re
    from types import EllipsisType, UnionType
    from typing import (
        Any,
        Literal,
        NoReturn,
    )
    import urllib.parse
    import uuid

    from .parsing import ParseConfig
    from .printing import PrintConfig
    from .stream import Stream
    from .types import (
        Binary,
        Bool,
        Decimal,
        Document,
        ExactValue,
        Hex,
        Infinity,
        NaN,
        Node,
        Null,
        Numberish,
        Octal,
        RawString,
        String,
        Stringish,
        Value,
    )
    from .unset import UnsetT as UnsetT

    type ParseVersion = Literal[1, 2, "auto"]
    type ParseResult[T] = tuple[T | None, int]

    type TagKey = str | None | EllipsisType | re.Pattern[str] | Callable[[str | None], bool]
    type NameKey = str | None | EllipsisType | re.Pattern[str] | Callable[[str | None], bool]
    type NodeKey = NameKey | tuple[TagKey, NameKey]

    type _ClassInfo = type | UnionType | tuple[_ClassInfo, ...]
    type TypeKey = EllipsisType | _ClassInfo
    type ValueKey = TagKey | tuple[TagKey, TypeKey]

    type KDLAny = Document | Node | KDLValue
    type KDLValue = (
        Value
        | Binary
        | Bool
        | Decimal
        | ExactValue
        | Hex
        | Infinity
        | NaN
        | Null
        | Numberish
        | Octal
        | RawString
        | String
        | Stringish
    )

    type KDLishValue = (
        KDLValue
        | None
        | bool
        | str
        | int
        | float
        | decimal.Decimal
        | datetime.time
        | datetime.date
        | datetime.datetime
        | ipaddress.IPv4Address
        | ipaddress.IPv6Address
        | urllib.parse.ParseResult
        | uuid.UUID
        | re.Pattern[str]
        | bytes
    )
