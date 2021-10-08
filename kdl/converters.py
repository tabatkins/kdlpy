from __future__ import annotations

from typing import Union, Any
import decimal
import datetime
import ipaddress
import urllib.parse
import uuid
import re
import base64


from . import types
from .errors import ParseError, ParseFragment
from .stream import Stream

KDLValue = Union[
    str,
    int,
    float,
    bool,
    None,
    decimal.Decimal,
    datetime.time,
    datetime.date,
    datetime.datetime,
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    urllib.parse.ParseResult,
    uuid.UUID,
    re.Pattern,
    bytes,
    "types.Value",
]


def isKdlValue(val: Any) -> bool:
    if val is None:
        return True
    return isinstance(
        val,
        (
            str,
            int,
            float,
            bool,
            decimal.Decimal,
            datetime.time,
            datetime.date,
            datetime.datetime,
            ipaddress.IPv4Address,
            ipaddress.IPv6Address,
            urllib.parse.ParseResult,
            uuid.UUID,
            re.Pattern,
            bytes,
            types.Value,
        ),
    )


def toNative(val: types.Value, pf: ParseFragment) -> KDLValue:
    if isinstance(val, types.Numberish):
        if val.tag == "i8":
            return i8(val, pf)
        if val.tag == "i16":
            return i16(val, pf)
        if val.tag == "i32":
            return i32(val, pf)
        if val.tag == "i64":
            return i64(val, pf)
        if val.tag == "u8":
            return u8(val, pf)
        if val.tag == "u16":
            return u16(val, pf)
        if val.tag == "u32":
            return u32(val, pf)
        if val.tag == "u64":
            return u64(val, pf)
        if val.tag in ("f32", "f64"):
            return val.value
        if val.tag in ("decimal64", "decimal128"):
            return decim(val, pf)
    if isinstance(val, types.Stringish):
        if val.tag == "date-time":
            return dateTime(val, pf)
        if val.tag == "time":
            return time(val, pf)
        if val.tag == "date":
            return date(val, pf)
        if val.tag == "decimal":
            return decim(val, pf)
        if val.tag == "ipv4":
            return ipv4(val, pf)
        if val.tag == "ipv6":
            return ipv6(val, pf)
        if val.tag == "url":
            return url(val, pf)
        if val.tag == "uuid":
            return _uuid(val, pf)
        if val.tag == "regex":
            return regex(val, pf)
        if val.tag == "base64":
            return b64(val, pf)
    return val


def i8(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 7
    if not (-limit <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in an i8.")
    return int(val.value)


def i16(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 15
    if not (-limit <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in an i16.")
    return int(val.value)


def i32(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 31
    if not (-limit <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in an i32.")
    return int(val.value)


def i64(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 63
    if not (-limit <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in an i64.")
    return int(val.value)


def u8(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 8
    if not (0 <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in a u8.")
    return int(val.value)


def u16(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 16
    if not (0 <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in a u16.")
    return int(val.value)


def u32(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 32
    if not (0 <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in a u32.")
    return int(val.value)


def u64(val: types.Numberish, pf: ParseFragment) -> int:
    limit = 2 ** 64
    if not (0 <= val.value < limit):
        raise pf.error(f"{val.value} doesn't fit in a u64.")
    return int(val.value)


def decim(val: types.Value, pf: ParseFragment) -> decimal.Decimal:
    if isinstance(val, types.Numberish):
        chars = pf.fragment.replace("_", "")
    else:
        chars = val.value
    try:
        return decimal.Decimal(chars)
    except decimal.InvalidOperation as e:
        raise pf.error(f"Couldn't parse a decimal from {pf.fragment}.")


def dateTime(val: types.Stringish, pf: ParseFragment) -> datetime.datetime:
    try:
        return datetime.datetime.fromisoformat(val.value)
    except ValueError as e:
        raise pf.error(f"Couldn't parse a date-time from {pf.fragment}.")


def time(val: types.Stringish, pf: ParseFragment) -> datetime.time:
    try:
        return datetime.time.fromisoformat(val.value)
    except ValueError as e:
        raise pf.error(f"Couldn't parse a date-time from {pf.fragment}.")


def date(val: types.Stringish, pf: ParseFragment) -> datetime.date:
    try:
        return datetime.date.fromisoformat(val.value)
    except ValueError as e:
        raise pf.error(f"Couldn't parse a date from {pf.fragment}.")


def ipv4(val: types.Stringish, pf: ParseFragment) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(val.value)
    except ipaddress.AddressValueError as e:
        raise pf.error(f"Couldn't parse an IPv4 address from {pf.fragment}.")


def ipv6(val: types.Stringish, pf: ParseFragment) -> ipaddress.IPv6Address:
    try:
        return ipaddress.IPv6Address(val.value)
    except ipaddress.AddressValueError:
        raise pf.error(f"Couldn't parse an IPv6 address from {pf.fragment}.")


def url(val: types.Stringish, pf: ParseFragment) -> urllib.parse.ParseResult:
    try:
        return urllib.parse.urlparse(val.value)
    except ValueError:
        raise pf.error(f"Couldn't parse a url from {pf.fragment}.")


def _uuid(val: types.Stringish, pf: ParseFragment) -> uuid.UUID:
    try:
        return uuid.UUID(val.value)
    except:
        raise pf.error(f"Couldn't parse a UUID from {pf.fragment}.")


def regex(val: types.Stringish, pf: ParseFragment) -> re.Pattern:
    try:
        return re.compile(val.value)
    except:
        raise pf.error(f"Couldn't parse a regex from {pf.fragment}.")


def b64(val: types.Stringish, pf: ParseFragment) -> bytes:
    try:
        return base64.b64decode(val.value.encode("utf-8"), validate=True)
    except:
        raise pf.error(f"Couldn't parse base64.")


def toKdlValue(val: Any) -> KDLValue:
    if isinstance(val, decimal.Decimal):
        return types.String(str(val), "decimal")
    if isinstance(val, datetime.datetime):
        return types.String(val.isoformat(), "date-time")
    if isinstance(val, datetime.time):
        return types.String(val.isoformat(), "time")
    if isinstance(val, datetime.date):
        return types.String(val.isoformat(), "date")
    if isinstance(val, ipaddress.IPv4Address):
        return types.String(str(val), "ipv4")
    if isinstance(val, ipaddress.IPv6Address):
        return types.String(str(val), "ipv6")
    if isinstance(val, urllib.parse.ParseResult):
        return types.String(urllib.parse.urlunparse(val), "url")
    if isinstance(val, uuid.UUID):
        return types.String(str(val), "uuid")
    if isinstance(val, re.Pattern):
        return types.RawString(val.pattern, "regex")
    if isinstance(val, bytes):
        return types.String(base64.b64encode(val).decode("utf-8"), "base-64")

    if isKdlValue(val):
        return val
    if not callable(getattr(val, "to_kdl", None)):
        raise Exception(
            f"Can't convert object to KDL for serialization. Got:\n{repr(val)}"
        )
    value = val.to_kdl()
    if not isKdlValue(value):
        raise Exception(
            f"Expected object to convert to KDL value or compatible primitive. Got:\n{repr(val)}"
        )
    return value
