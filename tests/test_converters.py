from __future__ import annotations

import datetime
import decimal
import ipaddress
import math
import re
import sys
import typing as t
import urllib.parse
import uuid

import pytest

import kdl

_ISIZE_LIMIT = sys.maxsize + 1

_INT_TAG_BOUNDS: list[tuple[str, int, int]] = [
    ("i8", -(2**7), (2**7) - 1),
    ("i16", -(2**15), (2**15) - 1),
    ("i32", -(2**31), (2**31) - 1),
    ("i64", -(2**63), (2**63) - 1),
    ("i128", -(2**127), (2**127) - 1),
    ("u8", 0, (2**8) - 1),
    ("u16", 0, (2**16) - 1),
    ("u32", 0, (2**32) - 1),
    ("u64", 0, (2**64) - 1),
    ("u128", 0, (2**128) - 1),
    ("isize", -_ISIZE_LIMIT, _ISIZE_LIMIT - 1),
    ("usize", 0, (2 * _ISIZE_LIMIT) - 1),
]


@pytest.mark.parametrize(("tag", "lo", "hi"), _INT_TAG_BOUNDS, ids=[row[0] for row in _INT_TAG_BOUNDS])
def test_integer_tag_bounds(tag: str, lo: int, hi: int) -> None:
    doc = kdl.parse(f"n ({tag}){lo} ({tag})0 ({tag}){hi} ({tag})0x10 ({tag})0o10 ({tag})0b10\n")
    args = doc.nodes[0].args
    assert args == [lo, 0, hi, 16, 8, 2]
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in args)

    # Exact integral float/scinot values within range succeed
    assert kdl.parse(f"n ({tag}){hi}.0\n").nodes[0].args[0] == hi
    assert kdl.parse(f"n ({tag})1.5e1\n").nodes[0].args[0] == 15

    # Out of range (both low and high) must fail with ParseError
    with pytest.raises(kdl.ParseError):
        kdl.parse(f"n ({tag}){lo - 1}\n")
    with pytest.raises(kdl.ParseError):
        kdl.parse(f"n ({tag}){hi + 1}\n")
    with pytest.raises(kdl.ParseError):
        kdl.parse(f"n ({tag})0x{'f' * 40}\n")


def test_integer_precision_and_invalid_inputs() -> None:
    u64Max = (2**64) - 1
    aboveF64 = (2**53) + 1
    assert kdl.parse(f"n (i64){aboveF64}\n").nodes[0].args[0] == aboveF64
    assert kdl.parse(f"n (u64){'0' * 5000}42\n").nodes[0].args[0] == 42
    assert kdl.parse("n (u8)0e10000000000000000000\n").nodes[0].args[0] == 0

    invalidCases = [
        f"n (i64){'9' * 400}\n",
        "n (u64)1e1000\n",
        "n (i8)1.5\n",
        "n (i8)-1.5\n",
        "n (u8)1e-1\n",
        f"n (u64){u64Max}.5\n",
        "n (u8)1e-10000000000000000000\n",
        "n (u8)#inf\n",
        "n (u8)#-inf\n",
        "n (u8)#nan\n",
    ]
    for src in invalidCases:
        with pytest.raises(kdl.ParseError):
            kdl.parse(src)


def test_integer_and_string_converters_in_kdl1_chunked_and_direct() -> None:
    u64Max = (2**64) - 1
    u128Max = (2**128) - 1
    isizeMin = -_ISIZE_LIMIT
    aboveF64 = (2**53) + 1
    big = "9" * 5000
    pad = "// " + ("x" * 140) + "\n"
    v1Cfg = kdl.ParseConfig(version=1)

    assert kdl.parse(f"{pad}n (u64){u64Max}\n", v1Cfg).nodes[0].args[0] == u64Max
    assert kdl.parse(f"{pad}n (u128){u128Max}\n", v1Cfg).nodes[0].args[0] == u128Max
    assert kdl.parse(f"{pad}n (isize){isizeMin}\n", v1Cfg).nodes[0].args[0] == isizeMin
    assert kdl.parse(f"{pad}n (i64){aboveF64}\n", v1Cfg).nodes[0].args[0] == aboveF64
    assert kdl.parse(f'{pad}n (base85)"BOu!rDZ"\n', v1Cfg).nodes[0].args[0] == b"hello"
    assert kdl.parse(f'{pad}n (url-reference)"/a"\n', v1Cfg).nodes[0].args[0] == urllib.parse.urlparse("/a")

    for src in ("n (i8)1.5\n", f"{pad}n (i8)1.5\n", f"{pad}n (u8)1e-1\n"):
        with pytest.raises(kdl.ParseError):
            kdl.parse(src, v1Cfg)

    for cfg in (kdl.ParseConfig(version=2), v1Cfg):
        for src in (f"{pad}n (u64){big}\n", f"{pad}n (u64)0x{'f' * 5000}\n"):
            with pytest.raises(kdl.ParseError):
                kdl.parse(src, cfg)


def test_float_and_decimal_converters() -> None:
    doc = kdl.parse(
        "n (f32)1.5 (f64)-2.25 (f64)0x10 (f32)#inf (f64)#-inf (f64)#nan "
        + f"(f64)0x{'f' * 5000} (f64)-0x{'f' * 5000}\n",
    )
    args = doc.nodes[0].args
    assert args[0] == 1.5 and isinstance(args[0], float)
    assert args[1] == -2.25 and isinstance(args[1], float)
    assert args[2] == 16.0 and isinstance(args[2], float)
    assert args[3] == float("inf")
    assert args[4] == float("-inf")
    assert math.isnan(args[5])
    assert args[6] == float("inf")
    assert args[7] == float("-inf")

    big = "9" * 5000
    decDoc = kdl.parse(
        "n (decimal64)1_234.56 (decimal128)-0.001 (decimal64)0x10 (decimal64)0o10 (decimal64)0b10 "
        + f'(decimal64)#inf (decimal128)#-inf (decimal64)#nan (decimal64){big} (decimal)"3.14159265358979323846"\n',
    )
    decArgs = decDoc.nodes[0].args
    assert decArgs[0] == decimal.Decimal("1234.56")
    assert decArgs[1] == decimal.Decimal("-0.001")
    assert decArgs[2] == decimal.Decimal(16)
    assert decArgs[3] == decimal.Decimal(8)
    assert decArgs[4] == decimal.Decimal(2)
    assert decArgs[5] == decimal.Decimal("Infinity")
    assert decArgs[6] == decimal.Decimal("-Infinity")
    assert decArgs[7].is_nan()
    assert decArgs[8] == decimal.Decimal(big)
    assert decArgs[9] == decimal.Decimal("3.14159265358979323846")

    with pytest.raises(kdl.ParseError):
        kdl.parse('n (decimal)"not-a-decimal"\n')


def test_builtin_string_tag_converters() -> None:
    doc = kdl.parse(
        'n (date-time)"2026-11-23T12:34:56+00:00" '
        + '(date)"2026-11-23" '
        + '(time)"12:34:56.5" '
        + '(ipv4)"192.0.2.1" '
        + '(ipv6)"2001:db8::1" '
        + '(url)"https://example.com/a?b=1#c" '
        + '(url-reference)"/a/b?x=1#f" '
        + '(irl)"https://例え.jp/パス" '
        + '(irl-reference)"/パス?q=値" '
        + '(uuid)"12345678-1234-5678-1234-567812345678" '
        + '(regex)#"^[a-z]+$"# '
        + '(base64)"aGVsbG8=" '
        + '(base85)"BOu!rDZ" '
        + '(base85)"<~BOu!rDZ~>"\n',
    )
    assert doc.nodes[0].args == [
        datetime.datetime(2026, 11, 23, 12, 34, 56, tzinfo=datetime.UTC),
        datetime.date(2026, 11, 23),
        datetime.time(12, 34, 56, 500000),
        ipaddress.IPv4Address("192.0.2.1"),
        ipaddress.IPv6Address("2001:db8::1"),
        urllib.parse.urlparse("https://example.com/a?b=1#c"),
        urllib.parse.urlparse("/a/b?x=1#f"),
        urllib.parse.urlparse("https://例え.jp/パス"),
        urllib.parse.urlparse("/パス?q=値"),
        uuid.UUID("12345678-1234-5678-1234-567812345678"),
        re.compile(r"^[a-z]+$"),
        b"hello",
        b"hello",
        b"hello",
    ]


@pytest.mark.parametrize(
    ("src", "expectedLoc"),
    [
        ("node (u8)999\n", (1, 10)),
        ('node (date-time)"bad"\n', (1, 17)),
        ('node (date)"""\nnot-a-date\n"""\n', (1, 12)),
        ('node (time)"99:99"\n', (1, 12)),
        ('node (ipv4)"999.0.0.1"\n', (1, 12)),
        ('node (ipv6)"not::an::ip"\n', (1, 12)),
        ('node (url)"http://[::1"\n', (1, 11)),
        ('node (uuid)"bad"\n', (1, 12)),
        ('node (regex)"[unclosed"\n', (1, 13)),
        ('node (base64)"***"\n', (1, 14)),
        ('node (base85)"~~~bad~~~"\n', (1, 14)),
    ],
)
def test_converter_error_locations_point_to_value_start(src: str, expectedLoc: tuple[int, int]) -> None:
    with pytest.raises(kdl.ParseError) as excInfo:
        kdl.parse(src)
    assert (excInfo.value.line, excInfo.value.col) == expectedLoc


@pytest.mark.parametrize(
    "tag",
    [
        "duration",
        "currency",
        "country-2",
        "country-3",
        "country-subdivision",
        "email",
        "idn-email",
        "hostname",
        "idn-hostname",
        "url-template",
    ],
)
def test_unsupported_reserved_tags_remain_kdl_string(tag: str) -> None:
    val = kdl.parse(f'n ({tag})"val"\n').nodes[0].args[0]
    assert isinstance(val, kdl.String)
    assert val.tag == tag
    assert val.value == "val"


def test_wrong_kind_for_builtin_tag_and_native_flags() -> None:
    # Wrong value kind for a known tag stays a tagged kdl.Value (no crash)
    doc = kdl.parse('n (i8)"42" (date)2026 (url)#true (base64)123\n')
    assert doc.nodes[0].args == [
        kdl.String("42", tag="i8"),
        kdl.Decimal(2026, tag="date"),
        kdl.Bool(True, tag="url"),
        kdl.Decimal(123, tag="base64"),
    ]

    # nativeTaggedValues=False preserves tagged values as kdl.Value while converting untagged
    noTagDoc = kdl.parse('n 1 (i8)2 (date)"2026-11-23"\n', kdl.ParseConfig(nativeTaggedValues=False))
    assert noTagDoc.nodes[0].args == [
        1.0,
        kdl.Decimal(2, tag="i8"),
        kdl.String("2026-11-23", tag="date"),
    ]

    # nativeUntaggedValues=False preserves untagged as kdl.Value while still converting tagged
    noUntagDoc = kdl.parse('n 1 (i8)2 (date)"2026-11-23"\n', kdl.ParseConfig(nativeUntaggedValues=False))
    assert noUntagDoc.nodes[0].args == [
        kdl.Decimal(1),
        2,
        datetime.date(2026, 11, 23),
    ]


def test_native_serialization_roundtrip() -> None:
    values: list[t.Any] = [
        None,
        True,
        False,
        "hello world",
        "bare",
        42.0,
        -3.5,
        float("inf"),
        float("-inf"),
        decimal.Decimal("12345678901234567890.12345"),
        datetime.datetime(2026, 11, 23, 15, 30, 45, tzinfo=datetime.UTC),
        datetime.date(2026, 11, 23),
        datetime.time(15, 30, 45),
        ipaddress.IPv4Address("10.0.0.1"),
        ipaddress.IPv6Address("::1"),
        urllib.parse.urlparse("https://example.com/path?k=v#frag"),
        uuid.UUID("87654321-4321-8765-4321-876543218765"),
        re.compile(r"(?i)^foo\d+$"),
        b"\x00\x01\x02hello\xff",
    ]
    doc = kdl.Document(
        [
            kdl.Node(
                "roundtrip",
                args=[*values, float("nan")],
                props={f"p{i}": v for i, v in enumerate(values)},
            ),
        ],
    )
    printed = doc.print()
    reparsed = kdl.parse(printed, kdl.ParseConfig(version=2))
    node = reparsed.nodes[0]
    assert node.args[:-1] == values
    assert math.isnan(node.args[-1])
    assert node.props == {f"p{i}": v for i, v in enumerate(values)}
