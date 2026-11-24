from __future__ import annotations

import base64
import binascii
import datetime
import decimal
import ipaddress
import math
import re
import sys
import urllib.parse
import uuid

from . import t, types
from .chars import NEG_INF, POS_INF
from .errors import ParseError

if t.TYPE_CHECKING:
    type _NumConverter = t.Callable[[t.Numberish, t.Stream, int, int], t.KDLishValue]
    type _StrConverter = t.Callable[[t.Stringish, t.Stream, int, int], t.KDLishValue]

# Reserved KDL 2.0 type annotations (spec §3.8):
# - §3.8.1 Numbers without decimals: i8, i16, i32, i64, i128, u8, u16, u32, u64, u128, isize, usize -> int (all supported)
# - §3.8.2 Numbers with decimals: f32, f64 -> float; decimal64, decimal128 -> decimal.Decimal (all supported)
# - §3.8.3 Strings:
#   Supported:
#     date-time, time, date -> datetime.datetime / time / date
#     decimal -> decimal.Decimal
#     ipv4, ipv6 -> ipaddress.IPv4Address / IPv6Address
#     url, url-reference, irl, irl-reference -> urllib.parse.ParseResult
#     uuid -> uuid.UUID
#     regex -> re.Pattern[str]
#     base64, base85 -> bytes
#   Unsupported (no matching Python stdlib type/validator; left as tagged kdl.String):
#     duration, currency, country-2, country-3, country-subdivision,
#     email, idn-email, hostname, idn-hostname, url-template


def toNative(val: t.Value, s: t.Stream, start: int, end: int) -> t.KDLishValue:
    """Convert a tagged `Value` to its Python native type if its tag is a built-in KDL type."""
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
    """Convert a KDL numeric value to a Python `float`, saturating overflows to ±inf."""
    v = val.value
    try:
        return float(v)
    except OverflowError:
        return POS_INF if v > 0 else NEG_INF


def _makeIntConverter(low: int, high: int, label: str) -> _NumConverter:
    """Build a range-checked integer converter accepting whole numbers in `[low, high)`."""

    def convert(val: t.Numberish, s: t.Stream, start: int, end: int) -> int:
        """Convert a numeric `Value` to an exact Python `int` within `[low, high)`."""
        if isinstance(val, types.Decimal):
            if val.exponent == 0 and not isinstance(val.mantissa, float):
                vInt = val.mantissa
                if not (low <= vInt < high):
                    raise ParseError(s, start, f"{s.chars[start:end]} doesn't fit in {label}.")
                return vInt
            # Reparse via decimal.Decimal to check exact integrality and bounds without float precision loss.
            raw = s.chars[start:end]
            chars = raw.replace("_", "") if "_" in raw else raw
            try:
                vDec = decimal.Decimal(chars)
            except decimal.InvalidOperation:
                # Extreme exponents can exceed Decimal's exponent limit; 0e<huge> is still exact 0.
                if val.mantissa == 0:
                    if not (low <= 0 < high):
                        raise ParseError(s, start, f"0 doesn't fit in {label}.") from None
                    return 0
                if val.exponent < 0:
                    raise ParseError(s, start, f"Fractional value {chars} isn't valid for {label}.") from None
                raise ParseError(s, start, f"{chars} doesn't fit in {label}.") from None
            if not vDec.is_finite():
                raise ParseError(s, start, f"{vDec} doesn't fit in {label}.")
            if vDec != vDec.to_integral_value():
                raise ParseError(s, start, f"Fractional value {vDec} isn't valid for {label}.")
            if not (low <= vDec < high):
                raise ParseError(s, start, f"{vDec} doesn't fit in {label}.")
            return int(vDec)
        if isinstance(val, (types.Hex, types.Octal, types.Binary)):
            vInt = val.value
            if not (low <= vInt < high):
                raise ParseError(s, start, f"{s.chars[start:end]} doesn't fit in {label}.")
            return vInt
        v = val.value
        if isinstance(v, float) and math.isfinite(v) and not v.is_integer():
            raise ParseError(s, start, f"Fractional value {v} isn't valid for {label}.")
        if not (low <= v < high):
            raise ParseError(s, start, f"{v} doesn't fit in {label}.")
        return int(v)

    return convert


_ISIZE_LIMIT = sys.maxsize + 1

_i8 = _makeIntConverter(-(2**7), 2**7, "an i8")
_i16 = _makeIntConverter(-(2**15), 2**15, "an i16")
_i32 = _makeIntConverter(-(2**31), 2**31, "an i32")
_i64 = _makeIntConverter(-(2**63), 2**63, "an i64")
_i128 = _makeIntConverter(-(2**127), 2**127, "an i128")
_u8 = _makeIntConverter(0, 2**8, "a u8")
_u16 = _makeIntConverter(0, 2**16, "a u16")
_u32 = _makeIntConverter(0, 2**32, "a u32")
_u64 = _makeIntConverter(0, 2**64, "a u64")
_u128 = _makeIntConverter(0, 2**128, "a u128")
_isize = _makeIntConverter(-_ISIZE_LIMIT, _ISIZE_LIMIT, "an isize")
_usize = _makeIntConverter(0, 2 * _ISIZE_LIMIT, "a usize")


def _decim(val: t.Value, s: t.Stream, start: int, end: int) -> decimal.Decimal:
    """Convert a decimal or string `Value` into an exact `decimal.Decimal`."""
    if isinstance(val, types.Decimal):
        # Use raw source text so decimal64/decimal128 preserve full precision instead of float rounding.
        raw = s.chars[start:end]
        chars: str | int | float = raw.replace("_", "") if "_" in raw else raw
    else:
        chars = val.value
    try:
        return decimal.Decimal(chars)
    except decimal.InvalidOperation as exc:
        raise ParseError(s, start, f"Couldn't parse a decimal from {s.chars[start:end]}.") from exc


def _dateTime(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.datetime:
    """Parse an ISO-8601 `date-time` string into a `datetime.datetime`."""
    try:
        return datetime.datetime.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, start, f"Couldn't parse a date-time from {s.chars[start:end]}.") from exc


def _time(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.time:
    """Parse an ISO-8601 `time` string into a `datetime.time`."""
    try:
        return datetime.time.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, start, f"Couldn't parse a time from {s.chars[start:end]}.") from exc


def _date(val: t.Stringish, s: t.Stream, start: int, end: int) -> datetime.date:
    """Parse an ISO-8601 `date` string into a `datetime.date`."""
    try:
        return datetime.date.fromisoformat(val.value)
    except ValueError as exc:
        raise ParseError(s, start, f"Couldn't parse a date from {s.chars[start:end]}.") from exc


def _ipv4(val: t.Stringish, s: t.Stream, start: int, end: int) -> ipaddress.IPv4Address:
    """Parse an `ipv4` string into an `ipaddress.IPv4Address`."""
    try:
        return ipaddress.IPv4Address(val.value)
    except ipaddress.AddressValueError as exc:
        raise ParseError(s, start, f"Couldn't parse an IPv4 address from {s.chars[start:end]}.") from exc


def _ipv6(val: t.Stringish, s: t.Stream, start: int, end: int) -> ipaddress.IPv6Address:
    """Parse an `ipv6` string into an `ipaddress.IPv6Address`."""
    try:
        return ipaddress.IPv6Address(val.value)
    except ipaddress.AddressValueError as exc:
        raise ParseError(s, start, f"Couldn't parse an IPv6 address from {s.chars[start:end]}.") from exc


def _url(val: t.Stringish, s: t.Stream, start: int, end: int) -> urllib.parse.ParseResult:
    """Parse a `url`/`irl` (or reference) string into a `urllib.parse.ParseResult`."""
    try:
        return t.cast("urllib.parse.ParseResult", urllib.parse.urlparse(val.value))
    except ValueError as exc:
        raise ParseError(s, start, f"Couldn't parse a url from {s.chars[start:end]}.") from exc


def _uuid(val: t.Stringish, s: t.Stream, start: int, end: int) -> uuid.UUID:
    """Parse a `uuid` string into a `uuid.UUID`."""
    try:
        return uuid.UUID(val.value)
    except ValueError as exc:
        raise ParseError(s, start, f"Couldn't parse a UUID from {s.chars[start:end]}.") from exc


def _regex(val: t.Stringish, s: t.Stream, start: int, end: int) -> re.Pattern[str]:
    """Compile a `regex` string into a `re.Pattern`."""
    try:
        return re.compile(val.value)
    except re.error as exc:
        raise ParseError(s, start, f"Couldn't parse a regex from {s.chars[start:end]}.") from exc


def _b64(val: t.Stringish, s: t.Stream, start: int, _end: int) -> bytes:
    """Decode a strictly validated `base64` string into `bytes`."""
    try:
        return base64.b64decode(val.value.encode("utf-8"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ParseError(s, start, "Couldn't parse base64.") from exc


def _b85(val: t.Stringish, s: t.Stream, start: int, _end: int) -> bytes:
    """Decode an Ascii85 / Adobe-framed `base85` string into `bytes`."""
    raw = val.value
    adobe = raw.startswith("<~") and raw.endswith("~>")
    try:
        return base64.a85decode(raw.encode("utf-8"), adobe=adobe)
    except ValueError as exc:
        raise ParseError(s, start, "Couldn't parse base85.") from exc


_NUMBER_CONVERTERS: dict[str, _NumConverter] = {
    "i8": _i8,
    "i16": _i16,
    "i32": _i32,
    "i64": _i64,
    "i128": _i128,
    "u8": _u8,
    "u16": _u16,
    "u32": _u32,
    "u64": _u64,
    "u128": _u128,
    "isize": _isize,
    "usize": _usize,
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
    "url-reference": _url,
    "irl": _url,
    "irl-reference": _url,
    "uuid": _uuid,
    "regex": _regex,
    "base64": _b64,
    "base85": _b85,
}

NATIVE_TAGS: frozenset[str] = frozenset(_NUMBER_CONVERTERS) | frozenset(_STRING_CONVERTERS)
