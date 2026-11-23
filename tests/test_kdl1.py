from __future__ import annotations

import datetime
import ipaddress
import math
import typing as t
from pathlib import Path

import pytest

import kdl
from kdl import kdl1

V1_TESTS_DIR = Path(__file__).resolve().parent.parent / "upstream_tests_v1" / "test_cases"
V1_INPUT_DIR = V1_TESTS_DIR / "input"
V1_GOLDEN_DIR = V1_TESTS_DIR / "expected_kdl"

_V1_INPUT_FILES = sorted(p.name for p in V1_INPUT_DIR.glob("*.kdl"))
_V1_GOLDEN_FILES = {p.name for p in V1_GOLDEN_DIR.glob("*.kdl")}

_V1_CFG = kdl.ParseConfig(nativeUntaggedValues=False, version=1)
_AUTO_CFG = kdl.ParseConfig(nativeUntaggedValues=False, version="auto")
_V2_CFG = kdl.ParseConfig(nativeUntaggedValues=False, version=2)
_PRINT_CFG = kdl.PrintConfig(
    indent="    ",
    respectRadix=False,
    respectStringType=False,
    exponent="E",
    sortProperties=True,
)


def _normalizeValue(val: t.Any) -> t.Any:
    if isinstance(val, kdl.Numberish):
        num = val.value
        if isinstance(num, float) and math.isnan(num):
            return ("num", val.tag, "nan")
        if isinstance(num, float) and num == 0.0:
            # Distinguish -0.0 from +0.0 only if negative sign bit is set
            return ("num", val.tag, math.copysign(1.0, num), 0.0)
        return ("num", val.tag, float(num))
    if isinstance(val, kdl.Stringish):
        return ("str", val.tag, val.value)
    if isinstance(val, kdl.Bool):
        return ("bool", val.tag, val.value)
    if isinstance(val, kdl.Null):
        return ("null", val.tag)
    return ("other", type(val).__name__, val)


def _normalizeNode(node: kdl.Node) -> tuple[t.Any, ...]:
    args = [_normalizeValue(v) for v in node.args]
    props = sorted((k, _normalizeValue(v)) for k, v in node.props.items())
    children = [_normalizeNode(c) for c in node.nodes]
    return (node.name, node.tag, args, props, children)


def _normalizeDoc(doc: kdl.Document) -> list[tuple[t.Any, ...]]:
    return [_normalizeNode(n) for n in doc.nodes]


def _spyKdl1Parse(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    origParse = kdl1.parse
    calls = [0]

    def wrapped(input: str, config: kdl.ParseConfig | None = None) -> kdl.Document:
        calls[0] += 1
        return origParse(input, config)

    monkeypatch.setattr(kdl1, "parse", wrapped)
    return calls


# ---------------------------------------------------------------------------
# 1. KDL 1.0 upstream spec test suite
# ---------------------------------------------------------------------------


def test_v1_upstream_corpus_present() -> None:
    assert _V1_INPUT_FILES, f"No upstream KDL1 test files found in {V1_INPUT_DIR}"


@pytest.mark.parametrize("filename", _V1_INPUT_FILES)
def test_kdl1_upstream(filename: str) -> None:
    inputText = (V1_INPUT_DIR / filename).read_text(encoding="utf-8")
    if filename not in _V1_GOLDEN_FILES:
        with pytest.raises(kdl.ParseError):
            kdl.parse(inputText, _V1_CFG)
        return

    goldenText = (V1_GOLDEN_DIR / filename).read_text(encoding="utf-8")
    goldenDoc = kdl.parse(goldenText, _V1_CFG)
    v1Doc = kdl.parse(inputText, _V1_CFG)
    autoDoc = kdl.parse(inputText, _AUTO_CFG)
    # Also verify printing as KDL2 and re-parsing strictly as KDL2 preserves the document.
    roundtripDoc = kdl.parse(v1Doc.print(_PRINT_CFG), _V2_CFG)
    roundtripRawDoc = kdl.parse(v1Doc.print(), _V2_CFG)

    expectedNorm = _normalizeDoc(goldenDoc)
    assert _normalizeDoc(v1Doc) == expectedNorm
    assert _normalizeDoc(autoDoc) == expectedNorm
    assert _normalizeDoc(roundtripDoc) == expectedNorm
    assert _normalizeDoc(roundtripRawDoc) == expectedNorm


# ---------------------------------------------------------------------------
# 2. KDL 1.0 constructs that MUST trigger opportunistic fallback and succeed
# ---------------------------------------------------------------------------

_KDL1_FALLBACK_CASES: list[tuple[str, str, t.Callable[[kdl.Document], bool]]] = [
    ("kw_arg_true", "node true\n", lambda d: d.nodes[0].args == [True]),
    ("kw_arg_false", "node false\n", lambda d: d.nodes[0].args == [False]),
    ("kw_arg_null", "node null\n", lambda d: d.nodes[0].args == [None]),
    ("kw_prop_true", "node k=true\n", lambda d: d.nodes[0].props == {"k": True}),
    ("kw_prop_false", "node k=false\n", lambda d: d.nodes[0].props == {"k": False}),
    ("kw_prop_null", "node k=null\n", lambda d: d.nodes[0].props == {"k": None}),
    (
        "kw_tagged_arg",
        "node (t)true\n",
        lambda d: isinstance(d.nodes[0].args[0], kdl.Bool) and d.nodes[0].args[0].tag == "t",
    ),
    (
        "kw_tagged_prop",
        "node k=(t)null\n",
        lambda d: isinstance(d.nodes[0].props["k"], kdl.Null) and d.nodes[0].props["k"].tag == "t",
    ),
    ("kw_slashdash_arg", "node /- true 1\n", lambda d: d.nodes[0].args == [1.0]),
    ("kw_slashdash_node", "/- node false\nok 1\n", lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok"),
    ("raw_arg", 'node r"foo\\n"\n', lambda d: d.nodes[0].args == ["foo\\n"]),
    ("raw_hash_arg", 'node r#"a"b"#\n', lambda d: d.nodes[0].args == ['a"b']),
    ("raw_multi_hash", 'node r###"a"##b"###\n', lambda d: d.nodes[0].args == ['a"##b']),
    ("raw_node_name", 'r"\\node" 1\n', lambda d: d.nodes[0].name == "\\node"),
    ("raw_hash_node_name", 'r#"no"de"# 1\n', lambda d: d.nodes[0].name == 'no"de'),
    ("raw_prop_key", 'node r"k\\n"=1\n', lambda d: d.nodes[0].props == {"k\\n": 1.0}),
    ("raw_prop_val", 'node k=r#"v"x"#\n', lambda d: d.nodes[0].props == {"k": 'v"x'}),
    ("raw_node_tag", '(r"tg")node\n', lambda d: d.nodes[0].tag == "tg"),
    ("raw_hash_node_tag", '(r#"t"g"#)node\n', lambda d: d.nodes[0].tag == 't"g'),
    (
        "raw_arg_tag",
        'node (r"tg")1\n',
        lambda d: isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == "tg",
    ),
    ("raw_in_child", 'parent {\n    r"ch" r#"v"#\n}\n', lambda d: d.nodes[0].nodes[0].name == "ch"),
    ("raw_slashdash", 'node /- r"skip" 2\n', lambda d: d.nodes[0].args == [2.0]),
    ("raw_slashdash_nospace", 'node /-r"skip" 2\n', lambda d: d.nodes[0].args == [2.0]),
    (
        "raw_slashdash_node_nospace",
        '/-r"skip"\nok 1\n',
        lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok",
    ),
    ("raw_multiline", 'node r"line1\nline2"\n', lambda d: d.nodes[0].args == ["line1\nline2"]),
    ("multiline_quoted", 'node "hello\nworld"\n', lambda d: d.nodes[0].args == ["hello\nworld"]),
    ("multiline_quoted_crlf", 'node "a\r\nb"\n', lambda d: d.nodes[0].args == ["a\r\nb"]),
    ("multiline_quoted_vtab", 'node "a\x0bb"\n', lambda d: d.nodes[0].args == ["a\x0bb"]),
    ("multiline_quoted_in_child", 'p {\n    c "a\nb"\n}\n', lambda d: d.nodes[0].nodes[0].args == ["a\nb"]),
    ("solidus_escape", 'node "a\\/b"\n', lambda d: d.nodes[0].args == ["a/b"]),
    ("solidus_in_node_name", '"a\\/b" 1\n', lambda d: d.nodes[0].name == "a/b"),
    ("solidus_in_tag", '("a\\/b")node\n', lambda d: d.nodes[0].tag == "a/b"),
    ("hash_in_node_name", 'foo#bar "x"\n', lambda d: d.nodes[0].name == "foo#bar"),
    ("hash_start_node_name", '#foo "x"\n', lambda d: d.nodes[0].name == "#foo"),
    ("hash_only_node_name", '# "x"\n', lambda d: d.nodes[0].name == "#"),
    ("double_hash_node_name", '## "x"\n', lambda d: d.nodes[0].name == "##"),
    ("hash_in_prop_key", "node a#b=1\n", lambda d: d.nodes[0].props == {"a#b": 1.0}),
    ("hash_start_prop_key", "node #foo=1\n", lambda d: d.nodes[0].props == {"#foo": 1.0}),
    ("hash_only_prop_key", "node #=1\n", lambda d: d.nodes[0].props == {"#": 1.0}),
    ("double_hash_prop_key", "node ##foo=1\n", lambda d: d.nodes[0].props == {"##foo": 1.0}),
    ("hash_kw_prop_key", "node #true=1\n", lambda d: d.nodes[0].props == {"#true": 1.0}),
    ("hash_kw_slashdash_nospace", "node /-#true=1 2\n", lambda d: d.nodes[0].args == [2.0]),
    ("hash_upper_kw_prop_key", "node #True=1\n", lambda d: d.nodes[0].props == {"#True": 1.0}),
    ("hash_node_tag", "(#tag)node\n", lambda d: d.nodes[0].tag == "#tag"),
    ("hash_in_tag", "(a#b)node\n", lambda d: d.nodes[0].tag == "a#b"),
    (
        "hash_arg_tag",
        "node (#tag)1\n",
        lambda d: isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == "#tag",
    ),
    (
        "hash_prop_tag",
        "node k=(#tag)1\n",
        lambda d: isinstance(d.nodes[0].props["k"], kdl.Decimal) and d.nodes[0].props["k"].tag == "#tag",
    ),
    ("confusable_node_name_inf", 'inf "x"\n', lambda d: d.nodes[0].name == "inf"),
    ("confusable_node_name_True", 'True "x"\n', lambda d: d.nodes[0].name == "True"),
    ("confusable_prop_key_inf", "node inf=1\n", lambda d: d.nodes[0].props == {"inf": 1.0}),
    ("confusable_prop_key_neginf", "node -inf=1\n", lambda d: d.nodes[0].props == {"-inf": 1.0}),
    ("confusable_prop_key_True", "node True=1\n", lambda d: d.nodes[0].props == {"True": 1.0}),
    ("confusable_tag_inf", "(inf)node\n", lambda d: d.nodes[0].tag == "inf"),
    ("dot_digit_node_name", '.0 "x"\n', lambda d: d.nodes[0].name == ".0"),
    ("signed_dot_digit_node_name", '-.5 "x"\n', lambda d: d.nodes[0].name == "-.5"),
    ("dot_digit_prop_key", "node .5=1\n", lambda d: d.nodes[0].props == {".5": 1.0}),
    ("dot_digit_node_tag", "(.0)node\n", lambda d: d.nodes[0].tag == ".0"),
    (
        "dot_digit_arg_tag",
        "node (.0)1\n",
        lambda d: isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == ".0",
    ),
    (
        "dot_digit_prop_tag",
        "node k=(.0)1\n",
        lambda d: isinstance(d.nodes[0].props["k"], kdl.Decimal) and d.nodes[0].props["k"].tag == ".0",
    ),
    ("mid_doc_bom", 'node \ufeff"x"\n', lambda d: d.nodes[0].args == ["x"]),
    ("double_leading_bom", '\ufeff\ufeffnode "x"\n', lambda d: d.nodes[0].name == "node"),
    ("comment_bom", "// comment\ufeff\nnode 1\n", lambda d: d.nodes[0].args == [1.0]),
    ("string_bom", 'node "a\ufeffb"\n', lambda d: d.nodes[0].args == ["a\ufeffb"]),
    ("slashdash_hash_node", "/- #node 1\nok 2\n", lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok"),
    ("slashdash_dot_node", "/- .0 1\nok 2\n", lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok"),
    ("slashdash_hash_tag_node", "/- (#tag)node 1\nok 2\n", lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok"),
    ("slashdash_dot_tag_node", "/- (.0)node 1\nok 2\n", lambda d: len(d.nodes) == 1 and d.nodes[0].name == "ok"),
    ("slashdash_dot_prop", "node /- .0=1 2\n", lambda d: d.nodes[0].args == [2.0]),
    ("slashdash_bare_hash_prop", "node /- #=1 2\n", lambda d: d.nodes[0].args == [2.0]),
    ("slashdash_hash_tag_arg", "node /- (#tag)1 2\n", lambda d: d.nodes[0].args == [2.0]),
    ("slashdash_dot_tag_arg", "node /- (.0)1 2\n", lambda d: d.nodes[0].args == [2.0]),
]


@pytest.mark.parametrize(
    ("source", "checkFn"),
    [(src, fn) for _, src, fn in _KDL1_FALLBACK_CASES],
    ids=[name for name, _, _ in _KDL1_FALLBACK_CASES],
)
def test_kdl1_opportunistic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    checkFn: t.Callable[[kdl.Document], bool],
) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    doc = kdl.parse(source)
    assert calls[0] == 1, f"Expected exactly 1 KDL1 fallback call, got {calls[0]}"
    assert checkFn(doc)


# ---------------------------------------------------------------------------
# 3. Non-KDL1 parse errors MUST NOT attempt KDL1 fallback (kdl1.parse calls == 0)
# ---------------------------------------------------------------------------

_NO_FALLBACK_ERRORS: list[tuple[str, str]] = [
    ("unclosed_quoted_string", 'node "unclosed\nother arg=1\n'),
    ("unclosed_raw_string", 'node #"unclosed'),
    ("unclosed_multiline_string", 'node """\nunclosed'),
    ("single_line_triple_quote", 'node """one line"""'),
    ("unclosed_block_comment", "node /* unclosed"),
    ("unclosed_child_block", "node { child"),
    ("no_space_before_arg", 'node"string"\n'),
    ("no_space_after_r_ident", 'bar"string"\n'),
    ("no_space_between_args", 'node "string"1\n'),
    ("no_space_between_props", 'node foo="value"bar=5\n'),
    ("missing_semicolon_after_child", "foo123{bar}foo weeee\n"),
    ("malformed_hex_empty", "node 0x\n"),
    ("malformed_hex_underscore", "node 0x_10\n"),
    ("malformed_hex_char", "node 0x10g10\n"),
    ("malformed_octal_char", "node 0o45678\n"),
    ("malformed_binary_char", "node 0bx01\n"),
    ("malformed_decimal_dot", "node 1.\n"),
    ("malformed_decimal_dot_exp", "node 1.e7\n"),
    ("malformed_decimal_two_dots", "node 1.0.0\n"),
    ("malformed_decimal_exp", "node 1e\n"),
    ("malformed_leading_dot_zero", "node .0\n"),
    ("malformed_leading_dot_one", "node .1\n"),
    ("malformed_numeric_ident", "node 0n\n"),
    ("malformed_signed_numeric_ident", "node +0n\n"),
    ("confusable_float_args", "floats inf -inf nan\n"),
    ("confusable_prop_val_neginf", "node k=-inf\n"),
    ("confusable_upper_arg", "node True\n"),
    ("keyword_prop_key_true", "node true=1\n"),
    ("keyword_prop_key_false", "node false=1\n"),
    ("keyword_prop_key_null", "node null=1\n"),
    ("keyword_node_name_true", 'true "a"\n'),
    ("keyword_node_name_false", 'false "a"\n'),
    ("keyword_node_name_null", 'null "a"\n'),
    ("keyword_tag_true", "(true)node\n"),
    ("invalid_escape_a", 'node "\\a"\n'),
    ("invalid_escape_x", 'node "\\x20"\n'),
    ("invalid_escape_eof", 'node "\\'),
    ("invalid_unicode_no_braces", 'node "\\u1234"\n'),
    ("invalid_unicode_empty", 'node "\\u{}"\n'),
    ("invalid_unicode_too_long", 'node "\\u{0012345}"\n'),
    ("invalid_unicode_surrogate", 'node "\\u{D800}"\n'),
    ("invalid_unicode_above_max", 'node "\\u{110000}"\n'),
    ("v2_raw_multiline_single_quote", 'node #"\nhello\n"#\n'),
    ("v2_raw_unbalanced_hashes", 'node ##"foo"#\n'),
    ("v2_nonraw_trailing_hash", 'node "foo"#\n'),
    ("v2_nonraw_double_quote_end", 'node "foo""\n'),
    ("v2_raw_just_triple_quote", 'node #"""#\n'),
    ("empty_node_tag", "()node\n"),
    ("space_node_tag", "( )node\n"),
    ("tag_no_value", "node (type)\n"),
    ("prop_tag_no_value", "node key=(type)\n"),
    ("junk_in_tag", "node (foo bar)1\n"),
    ("slashdash_in_tag", "(/-ty)node\n"),
    ("slashdash_before_eof", "node foo /-\n"),
    ("slashdash_before_semi", "node foo /-;\n"),
    ("slashdash_after_prop_key", "node key /- = 1\n"),
    ("slashdash_before_prop_val", "node key = /-1\n"),
    ("slashdash_two_child_blocks", "node { a } /- { b } { c }\n"),
    ("number_as_node_name", "123\n"),
    ("stray_close_brace", "}\n"),
    ("stray_semicolon", ";\n"),
    ("leading_equals", "=foo\n"),
    ("slash_in_bare_id", "foo123/bar weeee\n"),
    ("bracket_in_bare_id", "foo123[bar] weeee\n"),
    ("paren_in_bare_id", "foo123(bar) weeee\n"),
    ("backslash_in_bare_id", "foo123\\bar weeee\n"),
    ("unknown_hash_kw_arg", "node #foo\n"),
    ("unknown_hash_kw_prop_val", "node k=#foo\n"),
    ("upper_hash_kw_arg", "node #True\n"),
    ("upper_hash_kw_prop_val", "node k=#True\n"),
    ("converter_range_error", "node (u8)999\n"),
    ("converter_date_error", 'node (date)"not-a-date"\n'),
]


@pytest.mark.parametrize(
    "source",
    [src for _, src in _NO_FALLBACK_ERRORS],
    ids=[name for name, _ in _NO_FALLBACK_ERRORS],
)
def test_no_fallback_on_non_kdl1_errors(monkeypatch: pytest.MonkeyPatch, source: str) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    with pytest.raises(kdl.ParseError):
        kdl.parse(source)
    assert calls[0] == 0, f"KDL1 fallback should NOT have run (ran {calls[0]}x) for {source!r}"


# ---------------------------------------------------------------------------
# 4. Version markers, ParseConfig.version overrides, error selection, KDL1 edges
# ---------------------------------------------------------------------------


def test_marker_v1_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    doc = kdl.parse("/- kdl-version 1\nnode true\n")
    assert calls[0] == 1
    assert doc.nodes[0].args == [True]


def test_marker_v1_bom(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    doc = kdl.parse("\ufeff/- kdl-version 1\nnode false\n")
    assert calls[0] == 1
    assert doc.nodes[0].args == [False]


def test_marker_v1_rejects_v2_syntax() -> None:
    with pytest.raises(kdl.ParseError, match="Junk after node"):
        kdl.parse("/- kdl-version 1\nnode bare_arg\n")


def test_marker_v1_broken_raw_string_raises_v1_error() -> None:
    with pytest.raises(kdl.ParseError, match="raw string"):
        kdl.parse('/- kdl-version 1\nnode r"unclosed\n')


@pytest.mark.parametrize("src", ["/- kdl-version 2\nnode true\n", '/- kdl-version 2\nnode r"foo"\n'])
def test_marker_v2_disables_fallback(monkeypatch: pytest.MonkeyPatch, src: str) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    with pytest.raises(kdl.ParseError):
        kdl.parse(src)
    assert calls[0] == 0


def test_config_version2_overrides_marker_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    with pytest.raises(kdl.ParseError):
        kdl.parse("/- kdl-version 1\nnode true\n", kdl.ParseConfig(version=2))
    assert calls[0] == 0


def test_config_version1_overrides_marker_v2() -> None:
    doc = kdl.parse("/- kdl-version 2\nnode true\n", kdl.ParseConfig(version=1))
    assert doc.nodes[0].args == [True]


def test_invalid_config_version() -> None:
    with pytest.raises(ValueError, match="Invalid KDL version"):
        kdl.parse("node 1\n", kdl.ParseConfig(version=t.cast("t.Any", 3)))


def test_fallback_failure_reraises_v2_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    # Line 1 is KDL2-only bare arg; line 2 triggers KDL1 fallback via `true`.
    # KDL1 fails on line 1; must re-raise original KDL2 error on line 2.
    with pytest.raises(kdl.ParseError) as excInfo:
        kdl.parse("node1 bare_arg\nnode2 true\n")
    assert calls[0] == 1
    assert excInfo.value.line == 2
    assert "confusable with keywords" in excInfo.value.msg


def test_broken_raw_string_fallback_reraises_v2_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spyKdl1Parse(monkeypatch)
    with pytest.raises(kdl.ParseError, match="Expected a node terminator"):
        kdl.parse('node r"unclosed\n')
    assert calls[0] == 1


def test_config_converters_and_native_types_in_kdl1() -> None:
    seenVals: list[str] = []

    def customVal(val: kdl.Value, pf: kdl.ParseFragment) -> t.Any:
        seenVals.append(pf.fragment)
        if val.tag == "skip":
            return NotImplemented
        return f"custom:{val.value}"

    def customNode(node: kdl.Node, pf: kdl.ParseFragment) -> t.Any:
        if node.tag == "skip":
            return NotImplemented
        return kdl.Node(
            name=f"conv_{node.name}_{pf.fragment}",
            args=node.args,
            props=node.props,
            nodes=node.nodes,
        )

    cfg = kdl.ParseConfig(
        nativeUntaggedValues=True,
        nativeTaggedValues=True,
        valueConverters={
            "skip": customVal,
            "custom": customVal,
        },
        nodeConverters={
            ("skip", ...): customNode,
            ("wrap", ...): customNode,
        },
    )
    printCfg = kdl.PrintConfig(indent="  ")
    parser = kdl.Parser(cfg, printCfg)
    doc = parser.parse(
        '(wrap)item true (date)"2026-11-19" (ipv4)"10.0.0.1" (f64)1.5 (custom)"hi" (skip)"keep" '
        + 'dup=1 dup=2 /- (custom)"ignored"\n'
        + "/- kdl-version 1\n",
    )
    assert doc.printConfig is printCfg
    assert len(doc.nodes) == 1
    node = doc.nodes[0]
    assert node.name == "conv_item_(wrap)item"
    assert node.args == [
        True,
        datetime.date(2026, 11, 19),
        ipaddress.IPv4Address("10.0.0.1"),
        1.5,
        "custom:hi",
        kdl.String("keep", tag="skip"),
    ]
    assert node.props == {"dup": 2.0}
    assert seenVals == ['"hi"', '"keep"']


@pytest.mark.parametrize(
    "src",
    [
        "node 1 \\\r\n 2\n",
        "node 1 \\\r 2\n",
        "// " + ("x" * 140) + "\nnode 1 \\\r\n 2\n",
    ],
)
def test_kdl1_crlf_escline(src: str) -> None:
    doc = kdl.parse(src, kdl.ParseConfig(version=1))
    assert doc.nodes[0].args == [1.0, 2.0]
