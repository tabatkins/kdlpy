# pylint: skip-file
# Module for holding types, for easy importing into the rest of the codebase
from __future__ import annotations

# The only three things that should be available during runtime.
from typing import TYPE_CHECKING, cast, overload

if TYPE_CHECKING:
    from typing import (
        AbstractSet,
        Any,
        AnyStr,
        Awaitable,
        Callable,
        DefaultDict,
        Deque,
        FrozenSet,
        Generator,
        Generic,
        Iterable,
        Iterator,
        Literal,
        Mapping,
        MutableMapping,
        MutableSequence,
        NamedTuple,
        NewType,
        Protocol,
        Sequence,
        TextIO,
        TypeAlias,
        TypedDict,
        TypeGuard,
        TypeVar,
    )

    from _typeshed import SupportsKeysAndGetItem
    from typing_extensions import (
        NotRequired,
        Required,
    )

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
        Node,
        Null,
        Numberish,
        Octal,
        RawString,
        String,
        Stringish,
        Value,
    )

    NodeKey = str | tuple[str | None, str | None]

    KDLAny: TypeAlias = Document | Node | KDLValue
    KDLValue: TypeAlias = (
        Value | Binary | Bool | Decimal | ExactValue | Hex | Null | Numberish | Octal | RawString | String | Stringish
    )

    import datetime
    import decimal
    import ipaddress
    import re
    import urllib
    import uuid

    KDLishValue: TypeAlias = (
        KDLValue
        | None
        | bool
        | String
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
        | re.Pattern
        | bytes
    )
