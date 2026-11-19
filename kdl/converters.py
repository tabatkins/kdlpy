from __future__ import annotations

import base64
import datetime
import decimal
import ipaddress
import re
import urllib.parse
import uuid

from . import t, types
from .errors import ParseError

if t.TYPE_CHECKING:
    _NumConverter: t.TypeAlias = t.Callable[[t.Numberish, t.Stream, int, int], t.KDLishValue]
    _StrConverter: t.TypeAlias = t.Callable[[t.Stringish, t.Stream, int, int], t.KDLishValue]


def toNative(val: t.Value, s: t.Stream, start: int, end: int) -> t.KDLishValue:
    # Takes (s, start, end) instead of a ParseFragment to avoid slicing and allocating on valid values.
    tag = val.tag
    if tag is None:
        return val
    if isinstance(val, types.Numberish):
        numConv = _NUMBER_CONVERTERS.get(tag)
        if numConv is not None:
            return numConv(val, s, start, end)
    elif isinstance(val, types.Stringish):
        strConv = _STRING_CONVERTERS.get(tag)
        if strConv is not None:
            return strConv(val, s, start, end)
    return val


def _asFloat(val: t.Numberish, _s: t.Stream, _start: int, _end: int) -> float:
    return float(val.value)


def _makeIntConverter(low: int, high: int, label: str) -> _NumConverter:
    def convert(val: t.Numberish, s: t.Stream, _start: int, end: int) -> int:
        # Read once since Decimal.value is a computed property.
        v = val.value
        if not (low <= v < high):
            raise ParseError(s, end, f"{v} doesn't fit in {label}.")
        return int(v)

    return convert


_i8 = _makeIntConverter(-(2**7), 2**7, "an i8")
_i16 = _makeIntConverter(-(2**15), 2**15, "an i16")
_i32 = _makeIntConverter(-(2**31), 2**31, "an i32")
_i64 = _makeIntConverter(-(2**63), 2**63, "an i64")
_u8 = _makeIntConverter(0, 2**8, "a u8")
_u16 = _makeIntConverter(0, 2**16, "a u16")
_u32 = _makeIntConverter(0, 2**32, "a u32")
_u64 = _makeIntConverter(0, 2**64, "a u64")


def _decim(val: t.Value, s: t.Stream, start: int, end: int) -> decimal.Decimal:
    if isinstance(val, types.Numberish):
        raw = s.chars[start:end]
        chars = raw.replace("_", "") if "_" in raw else raw
    else:
        chars = val.value
    try:
        return decimal.Decimal(chars)
    except decimal.InvalidOperation as exc:
        raise ParseError(s, end, f"Couldn't parse a decimal from {s.chars[start:end]}.") from exc


def _dateTime(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, end, f"Couldn't parse a date-time from {s.chars[start:end]}.") from exc


def _time(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.time:
    try:
        return datetime.time.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, end, f"Couldn't parse a time from {s.chars[start:end]}.") from exc


def _date(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.date:
    try:
        return datetime.date.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, end, f"Couldn't parse a date from {s.chars[start:end]}.") from exc


def _ipv4(val: t.Stringish, s: t.Stream, start: int, end: int) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(val.value)
    except ipaddress.AddressValueError as exc:
        raise ParseError(s, end, f"Couldn't parse an IPv4 address from {s.chars[start:end]}.") from exc


def _ipv6(val: t.Stringish, s: t.Stream, start: int, end: int) -> ipaddress.IPv6Address:
    try:
        return ipaddress.IPv6Address(val.value)
    except ipaddress.AddressValueError as exc:
        raise ParseError(s, end, f"Couldn't parse an IPv6 address from {s.chars[start:end]}.") from exc


def _url(val: t.Stringish, s: t.Stream, start: int, end: int) -> urllib.parse.ParseResult:
    try:
        return t.cast("urllib.parse.ParseResult", urllib.parse.urlparse(val.value))
    except ValueError as exc:
        raise ParseError(s, end, f"Couldn't parse a url from {s.chars[start:end]}.") from exc


def _uuid(val: t.Stringish, s: t.Stream, start: int, end: int) -> uuid.UUID:
    try:
        return uuid.UUID(val.value)
    except Exception as exc:
        raise ParseError(s, end, f"Couldn't parse a UUID from {s.chars[start:end]}.") from exc


def _regex(val: t.Stringish, s: t.Stream, start: int, end: int) -> re.Pattern[str]:
    try:
        return re.compile(val.value)
    except Exception as exc:
        raise ParseError(s, end, f"Couldn't parse a regex from {s.chars[start:end]}.") from exc


def _b64(val: t.Stringish, s: t.Stream, _start: int, end: int) -> bytes:
    try:
        return base64.b64decode(val.value.encode("utf-8"), validate=True)
    except Exception as exc:
        raise ParseError(s, end, "Couldn't parse base64.") from exc


_NUMBER_CONVERTERS: dict[str, _NumConverter] = {
    "i8": _i8,
    "i16": _i16,
    "i32": _i32,
    "i64": _i64,
    "u8": _u8,
    "u16": _u16,
    "u32": _u32,
    "u64": _u64,
    "f32": _asFloat,
    "f64": _asFloat,
    "decimal64": _decim,
    "decimal128": _decim,
}

_STRING_CONVERTERS: dict[str, _StrConverter] = {
    "date-time": _dateTime,
    "time": _time,
    "date": _date,
    "decimal": _decim,
    "ipv4": _ipv4,
    "ipv6": _ipv6,
    "url": _url,
    "uuid": _uuid,
    "regex": _regex,
    "base64": _b64,
}

NATIVE_TAGS: frozenset[str] = frozenset(_NUMBER_CONVERTERS) | frozenset(_STRING_CONVERTERS)
