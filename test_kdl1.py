#!/usr/bin/env python

from __future__ import annotations

import argparse
import contextlib
import datetime
import decimal
import io
import ipaddress
import math
import os
import re
import sys
import tempfile
import typing as t
import urllib.parse

# Force the local kdl module into the environment
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import kdl  # pylint: disable=wrong-import-position
from kdl import kdl1  # pylint: disable=wrong-import-position

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
V1_TEST_DIR = os.path.join(THIS_DIR, "upstream_tests_v1", "test_cases", "input")
V1_GOLDEN_DIR = os.path.join(THIS_DIR, "upstream_tests_v1", "test_cases", "expected_kdl")


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


def findV1TestFiles() -> tuple[set[str], set[str]]:
    inputs: set[str] = set()
    goldens: set[str] = set()
    for _, _, filenames in os.walk(V1_TEST_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                inputs.add(filename)
    for _, _, filenames in os.walk(V1_GOLDEN_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                goldens.add(filename)
    return inputs, goldens


def runV1UpstreamSuite(verbose: int) -> tuple[int, int, list[str]]:
    inputs, goldens = findV1TestFiles()
    v1Cfg = kdl.ParseConfig(nativeUntaggedValues=False, version=1)
    autoCfg = kdl.ParseConfig(nativeUntaggedValues=False, version="auto")
    v2Cfg = kdl.ParseConfig(nativeUntaggedValues=False, version=2)
    printCfg = kdl.PrintConfig(
        indent="    ",
        respectRadix=False,
        respectStringType=False,
        exponent="E",
        sortProperties=True,
    )

    good = 0
    bad: list[str] = []
    for filename in sorted(inputs):
        inputPath = os.path.join(V1_TEST_DIR, filename)
        with open(inputPath, "r", encoding="utf-8") as fh:
            inputText = fh.read()

        if filename not in goldens:
            # Expected KDL1 parse failure under version=1
            try:
                doc = kdl.parse(inputText, v1Cfg)
            except kdl.ParseError:
                good += 1
                continue
            bad.append(f"v1_fail:{filename}")
            if verbose >= 2:
                print(f"Unexpected v1 parse success in {filename}: {doc.print(printCfg)!r}")
            continue

        goldenPath = os.path.join(V1_GOLDEN_DIR, filename)
        with open(goldenPath, "r", encoding="utf-8") as fh:
            goldenText = fh.read()

        try:
            goldenDoc = kdl.parse(goldenText, v1Cfg)
            v1Doc = kdl.parse(inputText, v1Cfg)
            autoDoc = kdl.parse(inputText, autoCfg)
            # Also verify printing as KDL2 and re-parsing strictly as KDL2 preserves the document.
            roundtripDoc = kdl.parse(v1Doc.print(printCfg), v2Cfg)
            roundtripRawDoc = kdl.parse(v1Doc.print(), v2Cfg)
        except kdl.ParseError as err:
            bad.append(f"v1_valid:{filename}")
            if verbose >= 2:
                print(f"Unexpected parse failure in {filename}: {err}\nInput: {inputText!r}")
            continue

        expectedNorm = _normalizeDoc(goldenDoc)
        if _normalizeDoc(v1Doc) != expectedNorm:
            bad.append(f"v1_mismatch:{filename}")
            if verbose >= 2:
                print(f"v1 AST mismatch in {filename}:\n  got: {v1Doc}\n  exp: {goldenDoc}")
            continue
        if _normalizeDoc(autoDoc) != expectedNorm:
            bad.append(f"auto_mismatch:{filename}")
            if verbose >= 2:
                print(f"auto AST mismatch in {filename}:\n  got: {autoDoc}\n  exp: {goldenDoc}")
            continue
        if _normalizeDoc(roundtripDoc) != expectedNorm or _normalizeDoc(roundtripRawDoc) != expectedNorm:
            bad.append(f"roundtrip_mismatch:{filename}")
            if verbose >= 2:
                print(
                    f"KDL2 roundtrip mismatch in {filename}:\n  printed: {v1Doc.print(printCfg)!r}\n"
                    + f"  rawPrinted: {v1Doc.print()!r}",
                )
            continue
        good += 1

    return good, len(inputs), bad


def _spyKdl1Parse() -> tuple[list[int], t.Callable[[], None]]:
    origParse = kdl1.parse
    calls = [0]

    def wrapped(input: str, config: kdl.ParseConfig | None = None) -> kdl.Document:
        calls[0] += 1
        return origParse(input, config)

    kdl1.parse = wrapped

    def restore() -> None:
        kdl1.parse = origParse

    return calls, restore


def runFallbackTests(verbose: int) -> tuple[int, int, list[str]]:
    passed = 0
    total = 0
    failures: list[str] = []

    def check(name: str, fn: t.Callable[[], None]) -> None:
        nonlocal passed, total
        total += 1
        try:
            fn()
            passed += 1
        except AssertionError as err:
            failures.append(name)
            if verbose >= 2:
                print(f"FAILED {name}: {err}")

    # 1. KDL1 constructs that MUST trigger opportunistic fallback and succeed
    kdl1FallbackCases: list[tuple[str, str, t.Callable[[kdl.Document], None]]] = [
        ("kw_arg_true", "node true\n", lambda d: _assert(d.nodes[0].args == [True])),
        ("kw_arg_false", "node false\n", lambda d: _assert(d.nodes[0].args == [False])),
        ("kw_arg_null", "node null\n", lambda d: _assert(d.nodes[0].args == [None])),
        ("kw_prop_true", "node k=true\n", lambda d: _assert(d.nodes[0].props == {"k": True})),
        ("kw_prop_false", "node k=false\n", lambda d: _assert(d.nodes[0].props == {"k": False})),
        ("kw_prop_null", "node k=null\n", lambda d: _assert(d.nodes[0].props == {"k": None})),
        (
            "kw_tagged_arg",
            "node (t)true\n",
            lambda d: _assert(isinstance(d.nodes[0].args[0], kdl.Bool) and d.nodes[0].args[0].tag == "t"),
        ),
        (
            "kw_tagged_prop",
            "node k=(t)null\n",
            lambda d: _assert(isinstance(d.nodes[0].props["k"], kdl.Null) and d.nodes[0].props["k"].tag == "t"),
        ),
        ("kw_slashdash_arg", "node /- true 1\n", lambda d: _assert(d.nodes[0].args == [1.0])),
        (
            "kw_slashdash_node",
            "/- node false\nok 1\n",
            lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok"),
        ),
        ("raw_arg", 'node r"foo\\n"\n', lambda d: _assert(d.nodes[0].args == ["foo\\n"])),
        ("raw_hash_arg", 'node r#"a"b"#\n', lambda d: _assert(d.nodes[0].args == ['a"b'])),
        ("raw_multi_hash", 'node r###"a"##b"###\n', lambda d: _assert(d.nodes[0].args == ['a"##b'])),
        ("raw_node_name", 'r"\\node" 1\n', lambda d: _assert(d.nodes[0].name == "\\node")),
        ("raw_hash_node_name", 'r#"no"de"# 1\n', lambda d: _assert(d.nodes[0].name == 'no"de')),
        ("raw_prop_key", 'node r"k\\n"=1\n', lambda d: _assert(d.nodes[0].props == {"k\\n": 1.0})),
        ("raw_prop_val", 'node k=r#"v"x"#\n', lambda d: _assert(d.nodes[0].props == {"k": 'v"x'})),
        ("raw_node_tag", '(r"tg")node\n', lambda d: _assert(d.nodes[0].tag == "tg")),
        ("raw_hash_node_tag", '(r#"t"g"#)node\n', lambda d: _assert(d.nodes[0].tag == 't"g')),
        (
            "raw_arg_tag",
            'node (r"tg")1\n',
            lambda d: _assert(
                isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == "tg",
            ),
        ),
        ("raw_in_child", 'parent {\n    r"ch" r#"v"#\n}\n', lambda d: _assert(d.nodes[0].nodes[0].name == "ch")),
        ("raw_slashdash", 'node /- r"skip" 2\n', lambda d: _assert(d.nodes[0].args == [2.0])),
        ("raw_slashdash_nospace", 'node /-r"skip" 2\n', lambda d: _assert(d.nodes[0].args == [2.0])),
        (
            "raw_slashdash_node_nospace",
            '/-r"skip"\nok 1\n',
            lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok"),
        ),
        ("raw_multiline", 'node r"line1\nline2"\n', lambda d: _assert(d.nodes[0].args == ["line1\nline2"])),
        (
            "multiline_quoted",
            'node "hello\nworld"\n',
            lambda d: _assert(d.nodes[0].args == ["hello\nworld"]),
        ),
        (
            "multiline_quoted_crlf",
            'node "a\r\nb"\n',
            lambda d: _assert(d.nodes[0].args == ["a\r\nb"]),
        ),
        (
            "multiline_quoted_vtab",
            'node "a\x0bb"\n',
            lambda d: _assert(d.nodes[0].args == ["a\x0bb"]),
        ),
        (
            "multiline_quoted_in_child",
            'p {\n    c "a\nb"\n}\n',
            lambda d: _assert(d.nodes[0].nodes[0].args == ["a\nb"]),
        ),
        ("solidus_escape", 'node "a\\/b"\n', lambda d: _assert(d.nodes[0].args == ["a/b"])),
        ("solidus_in_node_name", '"a\\/b" 1\n', lambda d: _assert(d.nodes[0].name == "a/b")),
        ("solidus_in_tag", '("a\\/b")node\n', lambda d: _assert(d.nodes[0].tag == "a/b")),
        ("hash_in_node_name", 'foo#bar "x"\n', lambda d: _assert(d.nodes[0].name == "foo#bar")),
        ("hash_start_node_name", '#foo "x"\n', lambda d: _assert(d.nodes[0].name == "#foo")),
        ("hash_only_node_name", '# "x"\n', lambda d: _assert(d.nodes[0].name == "#")),
        ("double_hash_node_name", '## "x"\n', lambda d: _assert(d.nodes[0].name == "##")),
        ("hash_in_prop_key", "node a#b=1\n", lambda d: _assert(d.nodes[0].props == {"a#b": 1.0})),
        ("hash_start_prop_key", "node #foo=1\n", lambda d: _assert(d.nodes[0].props == {"#foo": 1.0})),
        ("hash_only_prop_key", "node #=1\n", lambda d: _assert(d.nodes[0].props == {"#": 1.0})),
        ("double_hash_prop_key", "node ##foo=1\n", lambda d: _assert(d.nodes[0].props == {"##foo": 1.0})),
        ("hash_kw_prop_key", "node #true=1\n", lambda d: _assert(d.nodes[0].props == {"#true": 1.0})),
        ("hash_kw_slashdash_nospace", "node /-#true=1 2\n", lambda d: _assert(d.nodes[0].args == [2.0])),
        ("hash_upper_kw_prop_key", "node #True=1\n", lambda d: _assert(d.nodes[0].props == {"#True": 1.0})),
        ("hash_node_tag", "(#tag)node\n", lambda d: _assert(d.nodes[0].tag == "#tag")),
        ("hash_in_tag", "(a#b)node\n", lambda d: _assert(d.nodes[0].tag == "a#b")),
        (
            "hash_arg_tag",
            "node (#tag)1\n",
            lambda d: _assert(
                isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == "#tag",
            ),
        ),
        (
            "hash_prop_tag",
            "node k=(#tag)1\n",
            lambda d: _assert(
                isinstance(d.nodes[0].props["k"], kdl.Decimal) and d.nodes[0].props["k"].tag == "#tag",
            ),
        ),
        ("confusable_node_name_inf", 'inf "x"\n', lambda d: _assert(d.nodes[0].name == "inf")),
        ("confusable_node_name_True", 'True "x"\n', lambda d: _assert(d.nodes[0].name == "True")),
        ("confusable_prop_key_inf", "node inf=1\n", lambda d: _assert(d.nodes[0].props == {"inf": 1.0})),
        ("confusable_prop_key_neginf", "node -inf=1\n", lambda d: _assert(d.nodes[0].props == {"-inf": 1.0})),
        ("confusable_prop_key_True", "node True=1\n", lambda d: _assert(d.nodes[0].props == {"True": 1.0})),
        ("confusable_tag_inf", "(inf)node\n", lambda d: _assert(d.nodes[0].tag == "inf")),
        ("dot_digit_node_name", '.0 "x"\n', lambda d: _assert(d.nodes[0].name == ".0")),
        ("signed_dot_digit_node_name", '-.5 "x"\n', lambda d: _assert(d.nodes[0].name == "-.5")),
        ("dot_digit_prop_key", "node .5=1\n", lambda d: _assert(d.nodes[0].props == {".5": 1.0})),
        ("dot_digit_node_tag", "(.0)node\n", lambda d: _assert(d.nodes[0].tag == ".0")),
        (
            "dot_digit_arg_tag",
            "node (.0)1\n",
            lambda d: _assert(
                isinstance(d.nodes[0].args[0], kdl.Decimal) and d.nodes[0].args[0].tag == ".0",
            ),
        ),
        (
            "dot_digit_prop_tag",
            "node k=(.0)1\n",
            lambda d: _assert(
                isinstance(d.nodes[0].props["k"], kdl.Decimal) and d.nodes[0].props["k"].tag == ".0",
            ),
        ),
        ("mid_doc_bom", 'node \ufeff"x"\n', lambda d: _assert(d.nodes[0].args == ["x"])),
        ("double_leading_bom", '\ufeff\ufeffnode "x"\n', lambda d: _assert(d.nodes[0].name == "node")),
        ("comment_bom", "// comment\ufeff\nnode 1\n", lambda d: _assert(d.nodes[0].args == [1.0])),
        ("string_bom", 'node "a\ufeffb"\n', lambda d: _assert(d.nodes[0].args == ["a\ufeffb"])),
        ("slashdash_hash_node", "/- #node 1\nok 2\n", lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok")),
        ("slashdash_dot_node", "/- .0 1\nok 2\n", lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok")),
        (
            "slashdash_hash_tag_node",
            "/- (#tag)node 1\nok 2\n",
            lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok"),
        ),
        (
            "slashdash_dot_tag_node",
            "/- (.0)node 1\nok 2\n",
            lambda d: _assert(len(d.nodes) == 1 and d.nodes[0].name == "ok"),
        ),
        ("slashdash_dot_prop", "node /- .0=1 2\n", lambda d: _assert(d.nodes[0].args == [2.0])),
        ("slashdash_bare_hash_prop", "node /- #=1 2\n", lambda d: _assert(d.nodes[0].args == [2.0])),
        ("slashdash_hash_tag_arg", "node /- (#tag)1 2\n", lambda d: _assert(d.nodes[0].args == [2.0])),
        ("slashdash_dot_tag_arg", "node /- (.0)1 2\n", lambda d: _assert(d.nodes[0].args == [2.0])),
    ]

    for name, src, verify in kdl1FallbackCases:

        def _runFallback(source: str = src, checkFn: t.Callable[[kdl.Document], None] = verify) -> None:
            calls, restore = _spyKdl1Parse()
            try:
                doc = kdl.parse(source)
                _assert(calls[0] == 1, f"Expected exactly 1 KDL1 fallback call, got {calls[0]}")
                checkFn(doc)
            finally:
                restore()

        check(f"fallback_ok:{name}", _runFallback)

    # 2. Random / non-KDL1 parse errors MUST NOT attempt KDL1 fallback (kdl1.parse calls == 0!)
    noFallbackErrors: list[tuple[str, str]] = [
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

    for name, src in noFallbackErrors:

        def _runNoFallback(source: str = src) -> None:
            calls, restore = _spyKdl1Parse()
            try:
                raised = False
                try:
                    kdl.parse(source)
                except kdl.ParseError:
                    raised = True
                _assert(raised, f"Expected ParseError for {source!r}")
                _assert(calls[0] == 0, f"KDL1 fallback should NOT have run (ran {calls[0]}x) for {source!r}")
            finally:
                restore()

        check(f"no_fallback:{name}", _runNoFallback)

    # 3. Version marker behavior
    def _testMarkerV1Direct() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            doc = kdl.parse("/- kdl-version 1\nnode true\n")
            _assert(calls[0] == 1)
            _assert(doc.nodes[0].args == [True])
        finally:
            restore()

    def _testMarkerV1Bom() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            doc = kdl.parse("\ufeff/- kdl-version 1\nnode false\n")
            _assert(calls[0] == 1)
            _assert(doc.nodes[0].args == [False])
        finally:
            restore()

    def _testMarkerV1RejectsV2Syntax() -> None:
        try:
            kdl.parse("/- kdl-version 1\nnode bare_arg\n")
        except kdl.ParseError as err:
            # Should be the KDL1 ParseError ("Junk after node, before terminator.")
            _assert("Junk after node" in err.msg, f"Expected KDL1 error, got: {err.msg}")
            return
        msg = "Expected ParseError for KDL2 syntax under /- kdl-version 1"
        raise AssertionError(msg)

    def _testMarkerV1BrokenRawStringRaisesV1Error() -> None:
        try:
            kdl.parse('/- kdl-version 1\nnode r"unclosed\n')
        except kdl.ParseError as err:
            _assert("raw string" in err.msg, f"Expected KDL1 raw string error, got: {err.msg}")
            return
        msg = "Expected ParseError"
        raise AssertionError(msg)

    def _testMarkerV2DisablesFallback() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            for src in ("/- kdl-version 2\nnode true\n", '/- kdl-version 2\nnode r"foo"\n'):
                raised = False
                try:
                    kdl.parse(src)
                except kdl.ParseError:
                    raised = True
                _assert(raised, f"Expected ParseError for {src!r}")
                _assert(calls[0] == 0, f"Expected 0 KDL1 calls with /- kdl-version 2, got {calls[0]}")
        finally:
            restore()

    def _testConfigVersion2OverridesMarkerV1() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            raised = False
            try:
                kdl.parse("/- kdl-version 1\nnode true\n", kdl.ParseConfig(version=2))
            except kdl.ParseError:
                raised = True
            _assert(raised, "Expected ParseError when version=2 overrides /- kdl-version 1")
            _assert(calls[0] == 0, f"Expected 0 KDL1 calls when version=2, got {calls[0]}")
        finally:
            restore()

    def _testConfigVersion1OverridesMarkerV2() -> None:
        doc = kdl.parse("/- kdl-version 2\nnode true\n", kdl.ParseConfig(version=1))
        _assert(doc.nodes[0].args == [True])

    def _testInvalidConfigVersion() -> None:
        try:
            kdl.parse("node 1\n", kdl.ParseConfig(version=t.cast("t.Any", 3)))
        except ValueError:
            return
        msg = "Expected ValueError for invalid version"
        raise AssertionError(msg)

    # 4. Error selection when opportunistic fallback also fails
    def _testFallbackFailureReraisesV2Error() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            # Line 1 is KDL2-only bare arg; line 2 triggers KDL1 fallback via `true`.
            # KDL1 fails on line 1; must re-raise original KDL2 error on line 2!
            src = "node1 bare_arg\nnode2 true\n"
            try:
                kdl.parse(src)
            except kdl.ParseError as err:
                _assert(calls[0] == 1, f"Expected 1 KDL1 call, got {calls[0]}")
                _assert(err.line == 2, f"Expected KDL2 error on line 2, got line {err.line}: {err.msg}")
                _assert("confusable with keywords" in err.msg, f"Expected KDL2 message, got: {err.msg}")
                return
            msg = "Expected ParseError for mixed KDL2/KDL1 doc"
            raise AssertionError(msg)
        finally:
            restore()

    def _testBrokenRawStringFallbackReraisesV2Error() -> None:
        calls, restore = _spyKdl1Parse()
        try:
            try:
                kdl.parse('node r"unclosed\n')
            except kdl.ParseError as err:
                _assert(calls[0] == 1, f"Expected 1 KDL1 call, got {calls[0]}")
                _assert("Expected a node terminator" in err.msg, f"Expected KDL2 error, got: {err.msg}")
                return
            msg = "Expected ParseError"
            raise AssertionError(msg)
        finally:
            restore()

    # 5. ParseConfig options & Parser integration under KDL1 fallback
    def _testConfigConvertersAndNativeTypesInKdl1() -> None:
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
        _assert(doc.printConfig is printCfg)
        _assert(len(doc.nodes) == 1)
        node = doc.nodes[0]
        _assert(node.name == "conv_item_(wrap)item", f"Got node.name={node.name!r}")
        _assert(
            node.args
            == [
                True,
                datetime.date(2026, 11, 19),
                ipaddress.IPv4Address("10.0.0.1"),
                1.5,
                "custom:hi",
                kdl.String("keep", tag="skip"),
            ],
            f"Unexpected args: {node.args!r}",
        )
        _assert(node.props == {"dup": 2.0}, f"Unexpected props: {node.props!r}")
        _assert(seenVals == ['"hi"', '"keep"'], f"Slashdashed value should not invoke converter; saw {seenVals!r}")

    def _testNodeArgsPropsAndPrintConfig() -> None:
        doc = kdl.parse("n b=2 #null 1 a=#null c=3 #null\n")
        node = doc.nodes[0]
        _assert(node.args == [None, 1.0, None], f"Got args={node.args!r}")
        _assert(node.props == {"b": 2.0, "a": None, "c": 3.0}, f"Got props={node.props!r}")
        _assert(list(node.getArgs((..., float))) == [1.0])
        _assert(list(node.getProps((..., float))) == [("b", 2.0), ("c", 3.0)])
        _assert(doc.print(kdl.PrintConfig(printNullArgs=False)) == "n 1.0 b=2.0 a=#null c=3.0\n")
        _assert(doc.print(kdl.PrintConfig(printNullProps=False)) == "n #null 1.0 #null b=2.0 c=3.0\n")
        _assert(
            doc.print(kdl.PrintConfig(printNullArgs=False, printNullProps=False, sortProperties=True))
            == "n 1.0 b=2.0 c=3.0\n",
        )
        _assert(doc.print(kdl.PrintConfig(sortProperties=True)) == "n #null 1.0 #null a=#null b=2.0 c=3.0\n")

    def _testDisallowedLiteralCodepoints() -> None:
        v2Cfg = kdl.ParseConfig(version=2)
        cases = [
            'node "a\x00b"\n',
            'node "a\x7fb"\n',
            'node "a\u202eb"\n',
            'node "a\ufeffb"\n',
            'node #"a\x00b"#\n',
            'node #"a\ufeffb"#\n',
            'node """\na\x00b\n"""\n',
            'node #"""\na\u202eb\n"""#\n',
            "node // comment\x00here\n",
            "node \\ // comment\x00here\n 1\n",
            "node /* block\x00comment */\n",
            "node /* block\ufeffcomment */\n",
        ]
        for src in cases:
            raised = False
            try:
                kdl.parse(src, v2Cfg)
            except kdl.ParseError:
                raised = True
            _assert(raised, f"Expected ParseError for disallowed codepoint in {src!r}")

    def _testDecimalSciMantissaAndOverflow() -> None:
        rawCfg = kdl.ParseConfig(nativeUntaggedValues=False)
        d1 = kdl.parse("n 0.00001e3\n", rawCfg)
        _assert(d1.print() == "n 1e-2\n", f"Got {d1.print()!r}")
        _assert(d1.nodes[0].args[0].value == 0.01)
        _assert(kdl.parse("n (f64)0.00001e3\n").nodes[0].args[0] == 0.01)
        d2 = kdl.parse("n 10000000000000000.0e5\n", rawCfg)
        _assert(d2.print() == "n 1e+21\n", f"Got {d2.print()!r}")
        _assert(d2.nodes[0].args[0].value == 1e21)
        d3 = kdl.parse("n 0.00001\n", rawCfg)
        _assert(
            d3.print(kdl.PrintConfig(exponent="E")) == "n 1E-5\n",
            f"Got {d3.print(kdl.PrintConfig(exponent='E'))!r}",
        )
        _assert(math.isinf(kdl.Decimal(10**400).value))
        _assert(kdl.Decimal(float("nan")).print() == "#nan")
        _assert(kdl.Decimal(float("inf"), 2).print() == "#inf")
        # Negative zero underflow parity between native and raw Decimal
        negUnder = kdl.parse("n -1e-500\n").nodes[0].args[0]
        _assert(negUnder == 0.0 and math.copysign(1.0, negUnder) == -1.0)
        # Enormous integers (> 4300 digits) fall back to float cleanly instead of raising ValueError
        big = "9" * 5000
        bigDoc = kdl.parse(f"n {big} -{big} {'0' * 5000} -{'0' * 5000} 1{'0' * 5000}e-5000 1e{big} 1e-{big}\n", rawCfg)
        bigArgs = bigDoc.nodes[0].args
        _assert(bigArgs[0].value == float("inf") and bigArgs[0].print() == "#inf")
        _assert(bigArgs[1].value == float("-inf") and bigArgs[1].print() == "#-inf")
        _assert(bigArgs[2].value == 0.0 and math.copysign(1.0, bigArgs[2].value) == 1.0)
        _assert(bigArgs[3].value == 0.0 and math.copysign(1.0, bigArgs[3].value) == 1.0)
        _assert(bigArgs[4].value == 1.0)
        _assert(bigArgs[5].value == float("inf"))
        _assert(bigArgs[6].value == 0.0)
        # Native huge underflow and huge tagged decimals/ints
        bigUnder = kdl.parse(f"n -1{'0' * 5000}e-6000\n").nodes[0].args[0]
        _assert(bigUnder == 0.0 and math.copysign(1.0, bigUnder) == -1.0)
        _assert(kdl.parse(f"n (decimal64){big}\n").nodes[0].args[0] == decimal.Decimal(big))
        _assert(kdl.parse(f"n (u64){'0' * 5000}42\n").nodes[0].args[0] == 42)
        hugeHexDoc = kdl.parse(f"n 0x{'f' * 5000}\n", rawCfg)
        _assert(hugeHexDoc.print(kdl.PrintConfig(respectRadix=False)) == "n #inf\n")
        _assert(kdl.parse(f"n 0x{'f' * 5000}\n").print() == "n #inf\n")
        _assert(kdl.parse(f"n (f64)0x{'f' * 5000}\n").nodes[0].args[0] == float("inf"))
        _assert(kdl.parse(f"n (f64)-0x{'f' * 5000}\n").nodes[0].args[0] == float("-inf"))
        # Also verify KDL1 direct and chunked paths on enormous integers
        pad = "// " + ("x" * 140) + "\n"
        for src in (f"n {big} 1e{big}\n", f"{pad}n {big} 1e{big}\n"):
            dV1 = kdl.parse(src, kdl.ParseConfig(version=1, nativeUntaggedValues=False))
            _assert(dV1.nodes[0].args[0].value == float("inf") and dV1.nodes[0].args[1].value == float("inf"))
        for cfg in (kdl.ParseConfig(version=2), kdl.ParseConfig(version=1)):
            for src in (f"{pad}n (u64){big}\n", f"{pad}n (u64)0x{'f' * 5000}\n"):
                raised = False
                try:
                    kdl.parse(src, cfg)
                except kdl.ParseError:
                    raised = True
                _assert(raised, f"Expected ParseError for {src[:30]!r} under {cfg}")

    def _testIntAndDecimalConverters() -> None:
        u64Max = (2**64) - 1
        i64Max = (2**63) - 1
        i64Min = -(2**63)
        u128Max = (2**128) - 1
        i128Max = (2**127) - 1
        i128Min = -(2**127)
        isizeMax = sys.maxsize
        isizeMin = -(sys.maxsize + 1)
        usizeMax = (2 * (sys.maxsize + 1)) - 1
        aboveF64 = (2**53) + 1
        _assert(kdl.parse(f"n (u64){u64Max}\n").nodes[0].args[0] == u64Max)
        _assert(kdl.parse(f"n (i64){i64Max}\n").nodes[0].args[0] == i64Max)
        _assert(kdl.parse(f"n (i64){i64Min}\n").nodes[0].args[0] == i64Min)
        _assert(kdl.parse(f"n (u128){u128Max}\n").nodes[0].args[0] == u128Max)
        _assert(kdl.parse(f"n (i128){i128Max}\n").nodes[0].args[0] == i128Max)
        _assert(kdl.parse(f"n (i128){i128Min}\n").nodes[0].args[0] == i128Min)
        _assert(kdl.parse(f"n (isize){isizeMax}\n").nodes[0].args[0] == isizeMax)
        _assert(kdl.parse(f"n (isize){isizeMin}\n").nodes[0].args[0] == isizeMin)
        _assert(kdl.parse(f"n (usize){usizeMax}\n").nodes[0].args[0] == usizeMax)
        _assert(kdl.parse(f"n (i64){aboveF64}\n").nodes[0].args[0] == aboveF64)
        _assert(kdl.parse(f"n (u64){u64Max}.0\n").nodes[0].args[0] == u64Max)
        _assert(kdl.parse("n (i8)1.5e1\n").nodes[0].args[0] == 15)
        _assert(kdl.parse("n (decimal64)0x10\n").nodes[0].args[0] == decimal.Decimal(16))
        _assert(kdl.parse("n (decimal64)#inf\n").nodes[0].args[0] == decimal.Decimal("Infinity"))
        # Additional reserved string tags: url-reference, irl, irl-reference, base85; unsupported stay kdl.String
        _assert(
            kdl.parse('n (url-reference)"/a/b?x=1#f"\n').nodes[0].args[0] == urllib.parse.urlparse("/a/b?x=1#f"),
        )
        _assert(
            kdl.parse('n (irl)"https://例え.jp/パス"\n').nodes[0].args[0]
            == urllib.parse.urlparse("https://例え.jp/パス"),
        )
        _assert(
            kdl.parse('n (irl-reference)"/パス?q=値"\n').nodes[0].args[0] == urllib.parse.urlparse("/パス?q=値"),
        )
        _assert(kdl.parse('n (base85)"BOu!rDZ"\n').nodes[0].args[0] == b"hello")
        _assert(kdl.parse('n (base85)"<~BOu!rDZ~>"\n').nodes[0].args[0] == b"hello")
        for unsup in ("duration", "currency", "country-2", "email", "hostname", "url-template"):
            v = kdl.parse(f'n ({unsup})"val"\n').nodes[0].args[0]
            _assert(isinstance(v, kdl.String) and v.tag == unsup and v.value == "val")
        # Out-of-range huge int and fractional values must raise ParseError, not OverflowError or truncate
        for src in (
            f"n (u64){u64Max + 1}\n",
            f"n (i64){i64Max + 1}\n",
            f"n (u128){u128Max + 1}\n",
            f"n (i128){i128Max + 1}\n",
            f"n (i128){i128Min - 1}\n",
            f"n (isize){isizeMax + 1}\n",
            f"n (isize){isizeMin - 1}\n",
            f"n (usize){usizeMax + 1}\n",
            "n (usize)-1\n",
            'n (base85)"~~~bad~~~"\n',
            f"n (i64){'9' * 400}\n",
            "n (u64)1e1000\n",
            "n (i8)1.5\n",
            "n (i8)-1.5\n",
            "n (u8)1e-1\n",
            f"n (u64){u64Max}.5\n",
            "n (u8)1e-10000000000000000000\n",
            "n (u8)#inf\n",
            "n (u8)#nan\n",
        ):
            raised = False
            try:
                kdl.parse(src)
            except kdl.ParseError:
                raised = True
            _assert(raised, f"Expected ParseError for {src!r}")
        # Also verify KDL1 chunked (> 128 chars) and direct paths
        pad = "// " + ("x" * 140) + "\n"
        _assert(kdl.parse(f"{pad}n (u64){u64Max}\n", kdl.ParseConfig(version=1)).nodes[0].args[0] == u64Max)
        _assert(kdl.parse(f"{pad}n (u128){u128Max}\n", kdl.ParseConfig(version=1)).nodes[0].args[0] == u128Max)
        _assert(kdl.parse(f"{pad}n (isize){isizeMin}\n", kdl.ParseConfig(version=1)).nodes[0].args[0] == isizeMin)
        _assert(kdl.parse(f"{pad}n (i64){aboveF64}\n", kdl.ParseConfig(version=1)).nodes[0].args[0] == aboveF64)
        _assert(kdl.parse(f'{pad}n (base85)"BOu!rDZ"\n', kdl.ParseConfig(version=1)).nodes[0].args[0] == b"hello")
        _assert(
            kdl.parse(f'{pad}n (url-reference)"/a"\n', kdl.ParseConfig(version=1)).nodes[0].args[0]
            == urllib.parse.urlparse("/a"),
        )
        for src in ("n (i8)1.5\n", f"{pad}n (i8)1.5\n", f"{pad}n (u8)1e-1\n"):
            raised = False
            try:
                kdl.parse(src, kdl.ParseConfig(version=1))
            except kdl.ParseError:
                raised = True
            _assert(raised, f"Expected KDL1 ParseError for {src!r}")
        # Converter error locations must point to the start of the value (not end)
        for src, expectedLoc in (
            ("node (u8)999\n", (1, 10)),
            ('node (date)"""\nnot-a-date\n"""\n', (1, 12)),
            ('node (uuid)"bad"\n', (1, 12)),
            ('node (regex)"[unclosed"\n', (1, 13)),
            ('node (base64)"***"\n', (1, 14)),
        ):
            try:
                kdl.parse(src)
                msg = f"Expected ParseError for {src!r}"
                raise AssertionError(msg)
            except kdl.ParseError as err:
                _assert(
                    (err.line, err.col) == expectedLoc,
                    f"Expected {expectedLoc} for {src!r}, got {(err.line, err.col)}: {err.msg}",
                )

        def failConv(_val: kdl.Value, pf: kdl.ParseFragment) -> t.Any:
            msg = "custom fail"
            raise pf.error(msg)

        for cfg in (
            kdl.ParseConfig(version=2, valueConverters={"bad": failConv}),
            kdl.ParseConfig(version=1, valueConverters={"bad": failConv}),
        ):
            try:
                kdl.parse('node (bad)"abc"\n', cfg)
                msg = f"Expected ParseError for version={cfg.version}"
                raise AssertionError(msg)
            except kdl.ParseError as err:
                _assert(
                    (err.line, err.col) == (1, 11),
                    f"Expected (1, 11) for version={cfg.version}, got {(err.line, err.col)}",
                )

    def _testStringEscapingAndRawRoundtrip() -> None:
        v2Cfg = kdl.ParseConfig(version=2)
        for ch in ("\x0b", "\x85", "\u2028", "\u2029", "\x00", "\x1f", "\x7f", "\u202e", "\ufeff"):
            val = f"a{ch}b"
            for item in (val, kdl.RawString(val)):
                printed = kdl.Document([kdl.Node("n", args=[item])]).print()
                reparsed = kdl.parse(printed, v2Cfg)
                _assert(reparsed.nodes[0].args[0] == val, f"Roundtrip failed for {item!r}: {printed!r}")
        manyHashes = 'a"' + ("#" * 120) + "b"
        printedRaw = kdl.Document([kdl.Node("n", args=[kdl.RawString(manyHashes)])]).print()
        _assert(kdl.parse(printedRaw, v2Cfg).nodes[0].args[0] == manyHashes)
        # Bytes regex patterns cannot be serialized as KDL strings
        raised = False
        try:
            kdl.Node("n", args=[re.compile(b"abc")]).print()
        except TypeError:
            raised = True
        _assert(raised, "Expected TypeError serializing bytes re.Pattern")
        # Regex patterns with inline flags round-trip cleanly; external non-default flags raise ValueError
        for pat in (re.compile("foo"), re.compile("foo", re.UNICODE), re.compile("(?i)foo"), re.compile("(?a)foo")):
            printedPat = kdl.Document([kdl.Node("n", args=[pat])]).print()
            reparsedPat = kdl.parse(printedPat, v2Cfg).nodes[0].args[0]
            _assert(reparsedPat == pat, f"Regex roundtrip mismatch for {pat!r}: {printedPat!r} -> {reparsedPat!r}")
        for badPat in (
            re.compile("foo", re.IGNORECASE),
            re.compile("foo", re.ASCII),
            re.compile("(?i)foo", re.MULTILINE),
        ):
            raised = False
            try:
                kdl.Node("n", args=[badPat]).print()
            except ValueError:
                raised = True
            _assert(raised, f"Expected ValueError serializing {badPat!r}")

    def _testTypeErrorsForInvalidKeysAndObjects() -> None:
        class BadToKdl:
            def to_kdl(self) -> object:
                return object()

        badChecks: list[tuple[str, t.Callable[[], t.Any]]] = [
            ("unserializable_val", lambda: kdl.Node("n", args=[object()]).print()),
            ("bad_val_to_kdl", lambda: kdl.Node("n", args=[BadToKdl()]).print()),
            ("unserializable_node", lambda: kdl.Document([t.cast("kdl.Node", object())]).print()),
            ("bad_node_to_kdl", lambda: kdl.Document([t.cast("kdl.Node", BadToKdl())]).print()),
            ("node_matches_non_node", lambda: kdl.nodeMatchesKey(object(), "a")),
            ("node_matches_bad_to_kdl", lambda: kdl.nodeMatchesKey(BadToKdl(), "a")),
            ("node_key_tuple_short", lambda: kdl.nodeMatchesKey(kdl.Node("a"), t.cast("t.Any", ("a",)))),
            ("node_key_tuple_long", lambda: kdl.nodeMatchesKey(kdl.Node("a"), t.cast("t.Any", ("t", "a", "x")))),
            ("value_key_tuple_short", lambda: kdl.valueMatchesKey(kdl.String("a"), t.cast("t.Any", ("t",)))),
            ("value_key_tuple_long", lambda: kdl.valueMatchesKey(kdl.String("a"), t.cast("t.Any", ("t", str, "x")))),
            ("invalid_tag_key", lambda: kdl.tagMatchesKey("a", t.cast("t.Any", 123))),
            ("invalid_name_key", lambda: kdl.nameMatchesKey("a", t.cast("t.Any", 123))),
            ("invalid_type_key", lambda: kdl.typeMatchesKey("a", t.cast("t.Any", 123))),
        ]
        for label, fn in badChecks:
            raised = False
            try:
                fn()
            except TypeError:
                raised = True
            _assert(raised, f"Expected TypeError for {label}")

        for badInf in (0.0, 1.0, float("nan")):
            raised = False
            try:
                kdl.Infinity(badInf)
            except ValueError:
                raised = True
            _assert(raised, f"Expected ValueError for Infinity({badInf!r})")
        _assert(kdl.Infinity(float("inf")).print() == "#inf")
        _assert(kdl.Infinity(float("-inf")).print() == "#-inf")
        for badNan in (0.0, 1.0, float("inf"), float("-inf")):
            raised = False
            try:
                kdl.NaN(badNan)
            except ValueError:
                raised = True
            _assert(raised, f"Expected ValueError for NaN({badNan!r})")
        _assert(kdl.NaN().print() == "#nan")
        _assert(kdl.NaN(float("nan")).print() == "#nan")

    def _testCustomNodeGetItem() -> None:
        class CustomNode:
            def to_kdl(self) -> kdl.Node:
                return kdl.Node("custom", args=[42])

        custom = CustomNode()
        doc = kdl.Document([t.cast("kdl.Node", custom)])
        _assert(doc["custom"] is custom)
        _assert(doc.get("custom") is custom)
        parent = kdl.Node("p", nodes=[t.cast("kdl.Node", custom)])
        _assert(parent["custom"] is custom)
        _assert(parent.get("custom") is custom)

    def _testKdl1CrlfEscline() -> None:
        v1Cfg = kdl.ParseConfig(version=1)
        pad = "// " + ("x" * 140) + "\n"
        for src in ("node 1 \\\r\n 2\n", "node 1 \\\r 2\n", pad + "node 1 \\\r\n 2\n"):
            doc = kdl.parse(src, v1Cfg)
            _assert(doc.nodes[0].args == [1.0, 2.0], f"Unexpected args for {src!r}: {doc.nodes[0].args!r}")

    def _testStreamLocCr() -> None:
        sCr = kdl.stream.Stream("a\rb\rc", kdl.ParseConfig())
        _assert([sCr.loc(i) for i in range(5)] == [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1)])
        sCrlf = kdl.stream.Stream("a\r\nb", kdl.ParseConfig())
        _assert(sCrlf.loc(0) == (1, 1) and sCrlf.loc(3) == (2, 1))
        _assert(sCr.loc(-5) == (1, 1))

    def _testCliInPlaceAndParseError() -> None:
        origArgv = sys.argv
        with tempfile.TemporaryDirectory() as tmpDir:
            path = os.path.join(tmpDir, "in.kdl")
            outPath = os.path.join(tmpDir, "out.kdl")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('node  "hello"  b=2 a=1\n')
            try:
                sys.argv = ["kdlreformat", "--sort-properties", path, path]
                kdl.cli()
            finally:
                sys.argv = origArgv
            with open(path, encoding="utf-8") as fh:
                _assert(fh.read() == "node hello a=1 b=2\n")

            # --version 2 rejects KDL1 syntax with concise stderr + exit 1; --version 1 accepts it
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("node true\n")
            with open(outPath, "w", encoding="utf-8") as fh:
                fh.write("keep-me\n")
            stderrBuf = io.StringIO()
            exitCode: int | str | None = None
            try:
                sys.argv = ["kdlreformat", "--version", "2", path, outPath]
                with contextlib.redirect_stderr(stderrBuf):
                    kdl.cli()
            except SystemExit as exc:
                exitCode = exc.code
            finally:
                sys.argv = origArgv
            _assert(exitCode == 1, f"Expected exit code 1, got {exitCode!r}")
            _assert(
                "ParseError:" in stderrBuf.getvalue(),
                f"Expected ParseError on stderr, got {stderrBuf.getvalue()!r}",
            )
            with open(outPath, encoding="utf-8") as fh:
                _assert(fh.read() == "keep-me\n", "outfile should not be truncated on parse error")

            try:
                sys.argv = ["kdlreformat", "--version", "1", path, outPath]
                kdl.cli()
            finally:
                sys.argv = origArgv
            with open(outPath, encoding="utf-8") as fh:
                _assert(fh.read() == "node #true\n")

    extraChecks: list[tuple[str, t.Callable[[], None]]] = [
        ("marker_v1_direct", _testMarkerV1Direct),
        ("marker_v1_bom", _testMarkerV1Bom),
        ("marker_v1_rejects_v2", _testMarkerV1RejectsV2Syntax),
        ("marker_v1_error_is_v1", _testMarkerV1BrokenRawStringRaisesV1Error),
        ("marker_v2_disables_fallback", _testMarkerV2DisablesFallback),
        ("config_v2_overrides_marker_v1", _testConfigVersion2OverridesMarkerV1),
        ("config_v1_overrides_marker_v2", _testConfigVersion1OverridesMarkerV2),
        ("invalid_config_version", _testInvalidConfigVersion),
        ("fallback_failure_reraises_v2", _testFallbackFailureReraisesV2Error),
        ("broken_raw_fallback_reraises_v2", _testBrokenRawStringFallbackReraisesV2Error),
        ("config_converters_and_native_in_kdl1", _testConfigConvertersAndNativeTypesInKdl1),
        ("node_args_props_and_print_config", _testNodeArgsPropsAndPrintConfig),
        ("disallowed_literal_codepoints", _testDisallowedLiteralCodepoints),
        ("decimal_sci_mantissa_and_overflow", _testDecimalSciMantissaAndOverflow),
        ("int_and_decimal_converters", _testIntAndDecimalConverters),
        ("string_escaping_and_raw_roundtrip", _testStringEscapingAndRawRoundtrip),
        ("type_errors_for_invalid_keys_and_objects", _testTypeErrorsForInvalidKeysAndObjects),
        ("custom_node_getitem", _testCustomNodeGetItem),
        ("kdl1_crlf_escline", _testKdl1CrlfEscline),
        ("stream_loc_cr", _testStreamLocCr),
        ("cli_inplace_and_parse_error", _testCliInPlaceAndParseError),
    ]
    for name, fn in extraChecks:
        check(name, fn)

    return passed, total, failures


def _assert(cond: bool, msg: str = "Assertion failed") -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> bool:
    cli = argparse.ArgumentParser()
    cli.add_argument("-v", dest="verbose", action="count", default=0)
    options = cli.parse_args()

    v1Good, v1Total, v1Bad = runV1UpstreamSuite(options.verbose)
    fbGood, fbTotal, fbBad = runFallbackTests(options.verbose)
    allBad = v1Bad + fbBad

    if not allBad:
        print(f"Success, {v1Good}/{v1Total} KDL1 upstream tests and {fbGood}/{fbTotal} fallback tests passed.")
        return True

    print(f"Failure, {v1Good}/{v1Total} KDL1 upstream tests and {fbGood}/{fbTotal} fallback tests passed.")
    if options.verbose >= 1:
        for item in allBad:
            print("* " + item)
    return False


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
