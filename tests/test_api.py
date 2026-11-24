from __future__ import annotations

import enum
import math
import re
import typing as t

import pytest

import kdl
from kdl import stream as kdl_stream, types as kdl_types

# ---------------------------------------------------------------------------
# Parser & ParseConfig (KDL 2.0 & KDL 1.0)
# ---------------------------------------------------------------------------


def test_parser_config_and_print_config_precedence() -> None:
    parseCfg = kdl.ParseConfig(nativeUntaggedValues=False, version=2)
    printCfg = kdl.PrintConfig(indent="  ", semicolons=True)
    parser = kdl.Parser(parseCfg, printCfg)

    doc = parser.parse("parent {\n  child 1\n}\n")
    assert doc.printConfig is printCfg
    assert isinstance(doc.nodes[0].nodes[0].args[0], kdl.Decimal)
    assert doc.print() == "parent {\n  child 1;\n};\n"
    assert str(doc) == "parent {\n  child 1;\n};\n"

    # Explicit PrintConfig passed to doc.print() overrides doc.printConfig
    assert doc.print(kdl.PrintConfig(indent="\t")) == "parent {\n\tchild 1\n}\n"

    # Explicit ParseConfig passed to parser.parse() overrides parser.parseConfig
    overrideDoc = parser.parse("n 1\n", kdl.ParseConfig(nativeUntaggedValues=True, version=2))
    assert overrideDoc.nodes[0].args == [1]
    assert isinstance(overrideDoc.nodes[0].args[0], int)
    assert overrideDoc.printConfig is printCfg


def test_parser_defaults() -> None:
    doc = kdl.Parser().parse("a 1 1.5 #true #null str\n")
    assert doc.printConfig is None
    assert doc.nodes[0].args == [1, 1.5, True, None, "str"]
    assert isinstance(doc.nodes[0].args[0], int)
    assert isinstance(doc.nodes[0].args[1], float)


def test_kdl2_value_converters_and_fallthrough() -> None:
    seenFragments: list[str] = []

    def firstConverter(val: kdl.Value, pf: kdl.ParseFragment) -> t.Any:
        seenFragments.append(pf.fragment)
        if val.value == "pass":
            return NotImplemented
        return f"first:{val.tag}:{val.value}"

    def secondConverter(val: kdl.Value, _pf: kdl.ParseFragment) -> t.Any:
        return f"second:{val.value}"

    def untaggedIntConverter(val: kdl.Value, _pf: kdl.ParseFragment) -> t.Any:
        if val.value == 0:
            return NotImplemented
        return int(val.value) * 10

    cfg = kdl.ParseConfig(
        version=2,
        valueConverters={
            "custom": firstConverter,
            (re.compile(r"^cust"), str): secondConverter,
            ("custom", kdl.String): secondConverter,
            "u8": lambda val, _pf: NotImplemented if val.value == 7 else f"override:{val.value}",
            (None, kdl.Decimal): untaggedIntConverter,
        },
    )
    doc = kdl.parse(
        'n (custom)"hit" (custom)"pass" (u8)5 (u8)7 3 0 /- (custom)"slashdashed" k=(custom)"prop"\n',
        cfg,
    )
    node = doc.nodes[0]
    assert node.args == [
        "first:custom:hit",
        "second:pass",
        "override:5",
        7,  # NotImplemented fell through to built-in (u8) converter
        30,
        0,  # NotImplemented fell through to nativeUntaggedValues
    ]
    assert node.props == {"k": "first:custom:prop"}
    # Slashdashed values must not invoke converters
    assert seenFragments == ['"hit"', '"pass"', '"prop"']


def test_kdl2_node_converters_and_fallthrough() -> None:
    seen: list[str] = []

    def skipOrWrap(node: kdl.Node, pf: kdl.ParseFragment) -> t.Any:
        seen.append(pf.fragment)
        if node.name == "passthrough":
            return NotImplemented
        return kdl.Node(
            name=f"wrapped_{node.name}",
            args=[pf.fragment, *node.args],
            props=node.props,
            nodes=node.nodes,
        )

    def secondConverter(node: kdl.Node, _pf: kdl.ParseFragment) -> t.Any:
        return {"converted": node.name, "children": node.nodes}

    cfg = kdl.ParseConfig(
        version=2,
        nodeConverters={
            ("tag", ...): skipOrWrap,
            (..., "passthrough"): secondConverter,
            "leaf": lambda node, _pf: f"leaf:{node.args[0]}",
        },
    )
    doc = kdl.parse(
        '/- (tag)ignored 1\n(tag)box 42 {\n  leaf "ok"\n}\n(tag)passthrough {\n  leaf "nested"\n}\n',
        cfg,
    )
    assert seen == ["(tag)box", "(tag)passthrough"]
    assert len(doc.nodes) == 2
    box = doc.nodes[0]
    assert isinstance(box, kdl.Node)
    assert box.name == "wrapped_box"
    assert box.args == ["(tag)box", 42]
    assert box.nodes == ["leaf:ok"]
    assert doc.nodes[1] == {"converted": "passthrough", "children": ["leaf:nested"]}


@pytest.mark.parametrize("version", [2, 1])
def test_parse_fragment_error_locations(version: t.Literal[1, 2]) -> None:
    def failVal(_val: kdl.Value, pf: kdl.ParseFragment) -> t.Any:
        msg = f"bad val {pf.fragment}"
        raise pf.error(msg)

    def failNode(_node: kdl.Node, pf: kdl.ParseFragment) -> t.Any:
        msg = f"bad node {pf.fragment}"
        raise pf.error(msg)

    valCfg = kdl.ParseConfig(version=version, valueConverters={"bad": failVal})
    with pytest.raises(kdl.ParseError) as valExc:
        kdl.parse('node (bad)"abc"\n', valCfg)
    assert (valExc.value.line, valExc.value.col) == (1, 11)
    assert 'bad val "abc"' in valExc.value.msg

    nodeCfg = kdl.ParseConfig(version=version, nodeConverters={"badnode": failNode})
    with pytest.raises(kdl.ParseError) as nodeExc:
        kdl.parse("ok 1\n  badnode 2\n", nodeCfg)
    assert (nodeExc.value.line, nodeExc.value.col) == (2, 3)
    assert "bad node badnode" in nodeExc.value.msg


def test_parse_error_long_message_formatting() -> None:
    s = kdl_stream.Stream("node 1\n", kdl.ParseConfig())
    shortErr = kdl.ParseError(s, 0, "short")
    assert shortErr.msg == "1:1 short"
    longDetails = "x" * 90
    longErr = kdl.ParseError(s, 5, longDetails)
    assert longErr.msg == f"1:6\n  {longDetails}"


# ---------------------------------------------------------------------------
# Document & Node indexing, lookup, and custom .to_kdl() nodes
# ---------------------------------------------------------------------------


def test_document_and_node_lookup_methods() -> None:
    doc = kdl.parse(
        """
        (cfg)server "primary" port=8080 {
            route "/a"
            (auth)route "/b"
        }
        server "secondary" port=8081
        """,
    )
    assert doc["server"].args == ["primary"]
    assert doc[("cfg", "server")].args == ["primary"]
    assert doc[(None, "server")].args == ["secondary"]
    assert doc.get("missing") is None
    assert doc.get("missing", "fallback") == "fallback"
    with pytest.raises(KeyError):
        _ = doc["missing"]
    assert [n.args[0] for n in doc.getAll("server")] == ["primary", "secondary"]

    srv = doc["server"]
    assert srv["route"].args == ["/a"]
    assert srv[("auth", "route")].args == ["/b"]
    assert srv.get("missing") is None
    assert srv.get("missing", 123) == 123
    with pytest.raises(KeyError):
        _ = srv["missing"]
    assert [n.args[0] for n in srv.getAll("route")] == ["/a", "/b"]
    assert srv.matchesKey(("cfg", "server"))
    assert not srv.matchesKey(("other", "server"))


def test_node_call_get_entry_and_set_entry() -> None:
    node = kdl.parse('server "primary" #null port=8080 opt=#null\n').nodes[0]

    # Single-arg __call__ and getEntry behave identically for int args (positive/negative) and str props
    for getter in (node, node.getEntry):
        assert getter(0) == "primary"
        assert getter(1) is None
        assert getter(-1) is None
        assert getter(-2) == "primary"
        with pytest.raises(IndexError):
            getter(2)
        with pytest.raises(IndexError):
            getter(-3)

        assert getter("port") == 8080
        assert isinstance(getter("port"), int)
        assert getter("opt") is None
        with pytest.raises(KeyError):
            getter("missing")

        for badKey in (True, False, None, 1.5, ("port", int), slice(0, 1)):
            with pytest.raises(TypeError, match="Entry key must be an int or str"):
                getter(t.cast("t.Any", badKey))

    # Default argument is supported by getEntry() only (not direct node(...) calls)
    assert node.getEntry(1, "fallback") is None  # present #null is NOT treated as missing
    assert node.getEntry(2, "fallback") == "fallback"
    assert node.getEntry(2, None) is None
    assert node.getEntry(-3, "fallback") == "fallback"
    assert node.getEntry("opt", "fallback") is None  # present #null is NOT treated as missing
    assert node.getEntry("missing", "fallback") == "fallback"
    assert node.getEntry("missing", None) is None
    for badKey in (True, False, None, 1.5, ("port", int), slice(0, 1)):
        with pytest.raises(TypeError, match="Entry key must be an int or str"):
            node.getEntry(t.cast("t.Any", badKey), "default")
    with pytest.raises(TypeError):
        t.cast("t.Any", node)("missing", "fallback")

    # setEntry updates existing args/props, appends when int == len(args), and inserts new props
    node.setEntry(0, "secondary")
    assert node(0) == "secondary"
    node.setEntry(-1, "last")
    assert node(1) == "last"
    node.setEntry(2, "appended")
    assert node.args == ["secondary", "last", "appended"]
    with pytest.raises(IndexError):
        node.setEntry(4, "too-far")
    with pytest.raises(IndexError):
        node.setEntry(-4, "too-far-neg")

    node.setEntry("port", 9090)
    node.setEntry("tls", True)
    assert node("port") == 9090
    assert node("tls") is True

    empty = kdl.Node("empty")
    with pytest.raises(IndexError):
        empty.setEntry(1, "bad")
    with pytest.raises(IndexError):
        empty.setEntry(-1, "bad")
    empty.setEntry(0, "first")
    assert empty.args == ["first"]

    for badKey in (True, False, None, 1.5, ("port", int)):
        with pytest.raises(TypeError, match="Entry key must be an int or str"):
            node.setEntry(t.cast("t.Any", badKey), 123)


def test_node_get_args_get_props_and_print_config() -> None:
    doc = kdl.parse("n b=2 #null 1 a=#null c=3 #null\n")
    node = doc.nodes[0]
    assert node.args == [None, 1, None]
    assert node.props == {"b": 2, "a": None, "c": 3}
    assert list(node.getArgs((..., int))) == [1]
    assert list(node.getProps((..., int))) == [("b", 2), ("c", 3)]
    assert doc.print(kdl.PrintConfig(printNullArgs=False)) == "n 1 b=2 a=#null c=3\n"
    assert doc.print(kdl.PrintConfig(printNullProps=False)) == "n #null 1 #null b=2 c=3\n"
    assert doc.print(kdl.PrintConfig(printNullArgs=False, printNullProps=False, sortProperties=True)) == "n 1 b=2 c=3\n"
    assert doc.print(kdl.PrintConfig(sortProperties=True)) == "n #null 1 #null a=#null b=2 c=3\n"


def test_empty_document_and_node_print_indent() -> None:
    assert kdl.Document().print() == "\n"
    node = kdl.Node("item", args=[1])
    assert node.print(indentLevel=-3) == "item 1\n"
    assert node.print(kdl.PrintConfig(indent="  "), indentLevel=2) == "    item 1\n"
    assert str(node) == "item 1\n"

    # Deep nesting beyond initial indent cache depth (16) grows the indent table seamlessly
    deep = kdl.Node("leaf")
    for i in range(25):
        deep = kdl.Node(f"lvl{i}", nodes=[deep])
    printed = kdl.Document([deep]).print(kdl.PrintConfig(indent="  "))
    assert ("  " * 25) + "leaf\n" in printed
    assert kdl.parse(printed).print(kdl.PrintConfig(indent="  ")) == printed


def test_custom_to_kdl_nodes_and_values() -> None:
    class CustomNode:
        def __init__(self, name: str, val: t.Any) -> None:
            self.name = name
            self.val = val

        def to_kdl(self) -> kdl.Node:
            return kdl.Node(self.name, args=[self.val])

    class CustomVal:
        def __init__(self, inner: t.Any) -> None:
            self.inner = inner

        def to_kdl(self) -> t.Any:
            return self.inner

    class MyIntEnum(enum.IntEnum):
        TWO = 2

    custom = CustomNode("custom", CustomVal(kdl.ExactValue("42_000", tag="count")))
    doc = kdl.Document([t.cast("kdl.Node", custom)])
    assert doc["custom"] is custom
    assert doc.get("custom") is custom
    assert list(doc.getAll("custom")) == [custom]
    assert doc.print() == "custom (count)42_000\n"

    parent = kdl.Node(
        "p",
        args=[CustomVal(None), kdl.Null("t"), MyIntEnum.TWO],
        props={"a": CustomVal(None), "b": kdl.Null("t"), "c": CustomVal("ok")},
        nodes=[t.cast("kdl.Node", custom)],
    )
    assert parent["custom"] is custom
    assert parent.get("custom") is custom
    assert parent.print(kdl.PrintConfig(printNullArgs=False, printNullProps=False)) == (
        "p 2 c=ok {\n\tcustom (count)42_000\n}\n"
    )


# ---------------------------------------------------------------------------
# NodeKey / ValueKey / TagKey / NameKey / TypeKey matching
# ---------------------------------------------------------------------------


def test_tag_name_type_key_matching() -> None:
    # TagKey: ..., None, str, re.Pattern, Callable
    assert kdl.tagMatchesKey(None, ...)
    assert kdl.tagMatchesKey("foo", ...)
    assert kdl.tagMatchesKey(None, None)
    assert not kdl.tagMatchesKey("foo", None)
    assert kdl.tagMatchesKey("foo", "foo")
    assert not kdl.tagMatchesKey("bar", "foo")
    assert not kdl.tagMatchesKey(None, "foo")
    assert kdl.tagMatchesKey("foo123", re.compile(r"foo\d+"))
    assert not kdl.tagMatchesKey(None, re.compile(r".*"))
    assert kdl.tagMatchesKey("abc", lambda tag: tag is not None and len(tag) == 3)
    assert kdl.tagMatchesKey(None, lambda tag: tag is None)

    # NameKey: ..., None (matches any name), str, re.Pattern, Callable
    assert kdl.nameMatchesKey("anything", ...)
    assert kdl.nameMatchesKey("anything", None)
    assert kdl.nameMatchesKey(None, None)
    assert kdl.nameMatchesKey("node", "node")
    assert not kdl.nameMatchesKey("other", "node")
    assert kdl.nameMatchesKey("item-1", re.compile(r"item-\d+"))
    assert not kdl.nameMatchesKey(None, re.compile(r".*"))
    assert kdl.nameMatchesKey("xyz", lambda name: name == "xyz")

    # TypeKey: ..., type, UnionType, tuple of types
    assert kdl.typeMatchesKey("hi", ...)
    assert kdl.typeMatchesKey(123, int)
    assert not kdl.typeMatchesKey("hi", int)
    assert kdl.typeMatchesKey("hi", int | str)
    assert not kdl.typeMatchesKey(1.5, int | str)
    assert kdl.typeMatchesKey(1.5, (int, float))


def test_node_and_value_key_matching() -> None:
    taggedNode = kdl.Node("item", tag="meta")
    untaggedNode = kdl.Node("item")
    assert kdl.nodeMatchesKey(taggedNode, "item")
    assert kdl.nodeMatchesKey(taggedNode, ...)
    assert kdl.nodeMatchesKey(taggedNode, re.compile(r"^it"))
    assert kdl.nodeMatchesKey(taggedNode, lambda n: n == "item")
    assert kdl.nodeMatchesKey(taggedNode, ("meta", "item"))
    assert kdl.nodeMatchesKey(taggedNode, (..., "item"))
    assert not kdl.nodeMatchesKey(taggedNode, (None, "item"))
    assert kdl.nodeMatchesKey(untaggedNode, (None, "item"))
    assert kdl.nodeMatchesKey(taggedNode, (re.compile(r"^m"), lambda n: n == "item"))

    # ValueKey on kdl.Value instances and native untagged Python values
    taggedStr = kdl.String("hello", tag="greeting")
    untaggedStr = kdl.String("hello")
    assert taggedStr.matchesKey("greeting")
    assert taggedStr.matchesKey(("greeting", kdl.Stringish))
    assert not taggedStr.matchesKey(("greeting", kdl.Numberish))
    assert taggedStr.matchesKey((..., kdl.Value))
    assert not taggedStr.matchesKey(None)
    assert untaggedStr.matchesKey(None)
    assert untaggedStr.matchesKey((None, kdl.String))
    assert kdl.valueMatchesKey(42, None)
    assert kdl.valueMatchesKey(42, ...)
    assert kdl.valueMatchesKey(42, (None, int))
    assert kdl.valueMatchesKey(42, (..., int | float))
    assert not kdl.valueMatchesKey(42, "tag")
    assert not kdl.valueMatchesKey(42, (None, str))


def test_type_errors_for_invalid_keys_and_objects() -> None:
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
        with pytest.raises(TypeError, match=r".*"):
            fn()
        assert label


# ---------------------------------------------------------------------------
# Value subclasses, Infinity/NaN, ExactValue, Decimal, String/RawString
# ---------------------------------------------------------------------------


def test_value_subclasses_printing_and_base_methods() -> None:
    base = kdl.Value()
    with pytest.raises(NotImplementedError):
        _ = base.value
    with pytest.raises(NotImplementedError):
        base.print()

    ev = kdl.ExactValue("0xdead_beef")
    assert ev.value == "0xdead_beef"
    assert ev.print() == "0xdead_beef"
    assert str(ev) == "0xdead_beef"
    assert kdl.ExactValue("1.00", tag="money").print() == "(money)1.00"

    assert str(kdl.Binary(5)) == "0b101"
    assert kdl.Binary(5, tag="bits").print() == "(bits)0b101"
    assert kdl.Binary(5, tag="bits").print(kdl.PrintConfig(respectRadix=False)) == "(bits)5"
    assert kdl.Binary(10**5000).print(kdl.PrintConfig(respectRadix=False)) == "#inf"
    assert kdl.Binary(-(10**5000)).print(kdl.PrintConfig(respectRadix=False)) == "#-inf"

    assert str(kdl.Octal(8)) == "0o10"
    assert kdl.Octal(8, tag="perm").print() == "(perm)0o10"
    assert kdl.Octal(8).print(kdl.PrintConfig(respectRadix=False)) == "8"
    assert kdl.Octal(10**5000).print(kdl.PrintConfig(respectRadix=False)) == "#inf"

    assert str(kdl.Hex(26)) == "0x1a"
    assert kdl.Hex(26, tag="u8").print() == "(u8)0x1a"
    assert kdl.Hex(26).print(kdl.PrintConfig(respectRadix=False)) == "26"

    assert str(kdl.Bool(True)) == "#true"
    assert str(kdl.Bool(False, tag="flag")) == "(flag)#false"

    nullVal = kdl.Null()
    assert nullVal.value is None
    assert str(nullVal) == "#null"
    assert str(kdl.Null("opt")) == "(opt)#null"

    assert str(kdl.IdentString("bare")) == "bare"
    assert str(kdl.IdentString("bare", tag="t")) == "(t)bare"
    assert kdl.IdentString("bare", tag="t").print(kdl.PrintConfig(respectStringType=True)) == "(t)bare"
    assert kdl.IdentString("has space").print(kdl.PrintConfig(respectStringType=True)) == '"has space"'
    assert kdl.IdentString(r"a\b").print(kdl.PrintConfig(respectStringType=True)) == '"a\\\\b"'
    assert kdl.IdentString(r"a\b").print(kdl.PrintConfig(respectStringType="auto")) == '#"a\\b"#'

    assert str(kdl.String("bare")) == "bare"
    assert str(kdl.String("has space", tag="my tag")) == '("my tag")"has space"'
    assert kdl.String("bare").print(kdl.PrintConfig(respectStringType=True)) == '"bare"'
    assert kdl.String("bare", tag="t").print(kdl.PrintConfig(respectStringType=True)) == '(t)"bare"'
    assert kdl.String(r"a\b").print(kdl.PrintConfig(respectStringType="auto")) == '#"a\\b"#'
    assert kdl.String(r"a\b").print(kdl.PrintConfig(respectStringType=True)) == '"a\\\\b"'
    assert kdl.String(r"a\b").print(kdl.PrintConfig(respectStringType=False)) == '"a\\\\b"'

    assert str(kdl.RawString('a"b', tag="raw")) == '(raw)#"a"b"#'
    assert str(kdl.RawString('a"#b', tag="raw")) == '(raw)##"a"#b"##'
    assert kdl.RawString('a"b', tag="raw").print(kdl.PrintConfig(respectStringType=True)) == '(raw)#"a"b"#'
    assert kdl.RawString('a"b').print(kdl.PrintConfig(respectStringType=False)) == '"a\\"b"'
    assert kdl.RawString("bare").print(kdl.PrintConfig(respectStringType=False)) == "bare"
    assert kdl.RawString("").print() == '#""#'
    assert kdl.RawString("").print(kdl.PrintConfig(respectStringType=False)) == '""'
    assert kdl.RawString('"starts-with-quote').print() == '"\\"starts-with-quote"'

    # Native Python float specials and huge ints in Node args & props
    huge = 10**5000
    doc = kdl.Document(
        [
            kdl.Node(
                "n",
                args=[float("inf"), float("-inf"), float("nan"), huge, -huge],
                props={"p1": float("inf"), "p2": float("-inf"), "p3": float("nan"), "p4": huge, "p5": -huge},
            ),
        ],
    )
    assert doc.print() == "n #inf #-inf #nan #inf #-inf p1=#inf p2=#-inf p3=#nan p4=#inf p5=#-inf\n"


def test_infinity_and_nan_validation() -> None:
    for badInf in (0.0, 1.0, float("nan")):
        with pytest.raises(ValueError, match="Infinity value must be infinite"):
            kdl.Infinity(badInf)
    posInf = kdl.Infinity(float("inf"))
    negInf = kdl.Infinity(float("-inf"), tag="f64")
    assert str(posInf) == "#inf"
    assert str(negInf) == "(f64)#-inf"
    # Mutating to non-infinite is also caught on print()
    posInf.value = 1.0
    with pytest.raises(ValueError, match="Infinity value must be infinite"):
        posInf.print()

    for badNan in (0.0, 1.0, float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="NaN value must be NaN"):
            kdl.NaN(badNan)
    assert str(kdl.NaN()) == "#nan"
    assert str(kdl.NaN(float("nan"), tag="f64")) == "(f64)#nan"


def test_decimal_scinot_overflow_and_enormous_ints() -> None:
    rawCfg = kdl.ParseConfig(nativeUntaggedValues=False)
    d1 = kdl.parse("n 0.00001e3\n", rawCfg)
    assert d1.print() == "n 1e-2\n"
    assert d1.nodes[0].args[0].value == 0.01
    assert kdl.parse("n (f64)0.00001e3\n").nodes[0].args[0] == 0.01

    d2 = kdl.parse("n 10000000000000000.0e5\n", rawCfg)
    assert d2.print() == "n 1e+21\n"
    assert d2.nodes[0].args[0].value == 1e21

    d3 = kdl.parse("n 0.00001\n", rawCfg)
    assert d3.print(kdl.PrintConfig(exponent="E")) == "n 1E-5\n"

    assert kdl.Decimal(10**400).value == 10**400
    assert isinstance(kdl.Decimal(10**400).value, int)
    assert math.isinf(kdl.Decimal(1, 5000).value)
    assert math.isinf(kdl.Decimal(10**5000, 1).value)
    assert kdl.Decimal(0, 10**9).value == 0
    assert isinstance(kdl.Decimal(0, 10**9).value, int)
    assert kdl.Decimal(10**5000).print() == "#inf"
    assert kdl.Decimal(-(10**5000)).print() == "#-inf"
    assert kdl.Decimal(float("nan")).print() == "#nan"
    assert kdl.Decimal(float("inf"), 2).print() == "#inf"
    assert kdl.Decimal(float("-inf"), tag="x").print() == "(x)#-inf"

    # Negative zero underflow parity between native and raw Decimal
    negUnder = kdl.parse("n -1e-500\n").nodes[0].args[0]
    assert negUnder == 0.0 and math.copysign(1.0, negUnder) == -1.0

    # Enormous integers (> 4300 digits) fall back to float cleanly instead of raising ValueError
    big = "9" * 5000
    bigDoc = kdl.parse(f"n {big} -{big} {'0' * 5000} -{'0' * 5000} 1{'0' * 5000}e-5000 1e{big} 1e-{big}\n", rawCfg)
    bigArgs = bigDoc.nodes[0].args
    assert bigArgs[0].value == float("inf") and bigArgs[0].print() == "#inf"
    assert bigArgs[1].value == float("-inf") and bigArgs[1].print() == "#-inf"
    assert bigArgs[2].value == 0.0 and math.copysign(1.0, bigArgs[2].value) == 1.0
    assert bigArgs[3].value == 0.0 and math.copysign(1.0, bigArgs[3].value) == 1.0
    assert bigArgs[4].value == 1.0
    assert bigArgs[5].value == float("inf")
    assert bigArgs[6].value == 0.0

    bigUnder = kdl.parse(f"n -1{'0' * 5000}e-6000\n").nodes[0].args[0]
    assert bigUnder == 0.0 and math.copysign(1.0, bigUnder) == -1.0
    hugeHexDoc = kdl.parse(f"n 0x{'f' * 5000}\n", rawCfg)
    assert hugeHexDoc.print(kdl.PrintConfig(respectRadix=False)) == "n #inf\n"
    assert kdl.parse(f"n 0x{'f' * 5000}\n").print() == "n #inf\n"

    # KDL1 direct and chunked paths on enormous integers
    pad = "// " + ("x" * 140) + "\n"
    for src in (f"n {big} 1e{big}\n", f"{pad}n {big} 1e{big}\n"):
        dV1 = kdl.parse(src, kdl.ParseConfig(version=1, nativeUntaggedValues=False))
        assert dV1.nodes[0].args[0].value == float("inf")
        assert dV1.nodes[0].args[1].value == float("inf")


def test_integer_vs_float_number_parsing() -> None:
    src = (
        "n 0 -0 1 -42 +1_000 1__0 1e0 1e+0 1e-0 1e3 2_5E+2 -3e2 0e999999 "
        + "1.0 -0.0 1.5 1.5e2 1.0e0 1e-2 100e-2 -0e-5 "
        + "p_int=7 p_exp=2e4 p_flt=7.0 p_negexp=7e-1\n"
    )
    expectedArgs: list[tuple[float | int, type]] = [
        (0, int),
        (0, int),
        (1, int),
        (-42, int),
        (1000, int),
        (10, int),
        (1, int),
        (1, int),
        (1, int),
        (1000, int),
        (2500, int),
        (-300, int),
        (0, int),
        (1.0, float),
        (-0.0, float),
        (1.5, float),
        (150.0, float),
        (1.0, float),
        (0.01, float),
        (1.0, float),
        (0.0, float),
    ]
    expectedProps: dict[str, tuple[float | int, type]] = {
        "p_int": (7, int),
        "p_exp": (20000, int),
        "p_flt": (7.0, float),
        "p_negexp": (0.7, float),
    }

    pad = "// " + ("x" * 140) + "\n"
    for version in (2, 1):
        for prefix in ("", pad):
            for nativeUntagged in (True, False):
                cfg = kdl.ParseConfig(version=t.cast("kdl.ParseVersion", version), nativeUntaggedValues=nativeUntagged)
                doc = kdl.parse(prefix + src, cfg)
                node = doc.nodes[0]
                actualArgs = node.args if nativeUntagged else [v.value for v in node.args]
                actualProps = node.props if nativeUntagged else {k: v.value for k, v in node.props.items()}
                for actual, (expVal, expType) in zip(actualArgs, expectedArgs, strict=True):
                    assert actual == expVal
                    assert isinstance(actual, expType) and not isinstance(actual, bool)
                # Preserve negative zero on -0.0 and positive zero on -0e-5
                assert math.copysign(1.0, actualArgs[14]) == -1.0
                assert math.copysign(1.0, actualArgs[20]) == 1.0
                for propKey, (expVal, expType) in expectedProps.items():
                    assert actualProps[propKey] == expVal
                    assert isinstance(actualProps[propKey], expType) and not isinstance(actualProps[propKey], bool)

    # Large exact integer beyond float range stays exact int, while >4300-digit exponent falls back to inf
    bigExact = kdl.parse("n 1e400 1e5000\n", kdl.ParseConfig(version=2)).nodes[0].args
    assert bigExact[0] == 10**400 and isinstance(bigExact[0], int)
    assert bigExact[1] == float("inf") and isinstance(bigExact[1], float)


@pytest.mark.parametrize(
    "src",
    [
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
    ],
)
def test_disallowed_literal_codepoints(src: str) -> None:
    with pytest.raises(kdl.ParseError):
        kdl.parse(src, kdl.ParseConfig(version=2))


def test_string_escaping_raw_roundtrip_and_regex_flags() -> None:
    v2Cfg = kdl.ParseConfig(version=2)
    for ch in ("\x0b", "\x85", "\u2028", "\u2029", "\x00", "\x1f", "\x7f", "\u202e", "\ufeff"):
        val = f"a{ch}b"
        for item in (val, kdl.RawString(val)):
            printed = kdl.Document([kdl.Node("n", args=[item])]).print()
            reparsed = kdl.parse(printed, v2Cfg)
            assert reparsed.nodes[0].args[0] == val

    manyHashes = 'a"' + ("#" * 120) + "b"
    printedRaw = kdl.Document([kdl.Node("n", args=[kdl.RawString(manyHashes)])]).print()
    assert kdl.parse(printedRaw, v2Cfg).nodes[0].args[0] == manyHashes

    # Bytes regex patterns cannot be serialized as KDL strings
    with pytest.raises(TypeError):
        kdl.Node("n", args=[re.compile(b"abc")]).print()
    assert not kdl_types.isKdlishValue(re.compile(b"abc"))
    assert kdl_types.isKdlishValue(re.compile("abc"))

    # Regex patterns with inline flags round-trip cleanly; external non-default flags raise ValueError
    for pat in (re.compile("foo"), re.compile("foo", re.UNICODE), re.compile("(?i)foo"), re.compile("(?a)foo")):
        printedPat = kdl.Document([kdl.Node("n", args=[pat])]).print()
        reparsedPat = kdl.parse(printedPat, v2Cfg).nodes[0].args[0]
        assert reparsedPat == pat
    for badPat in (
        re.compile("foo", re.IGNORECASE),
        re.compile("foo", re.ASCII),
        re.compile("(?i)foo", re.MULTILINE),
    ):
        with pytest.raises(ValueError, match="non-default flags"):
            kdl.Node("n", args=[badPat]).print()


def test_kdl2_string_types_and_respect_string_type_modes() -> None:
    rawCfg = kdl.ParseConfig(nativeUntaggedValues=False, nativeTaggedValues=False, version=2)
    doc = kdl.parse(
        'n bare "quoted" #"raw"# ##"a"#b"## #""# (t)#"tagged"# (t)"quoted" (t)ident '
        + '#"""\n  line1\n  line2\n  """# '
        + 'p1=#"praw"# p2=(t)#"ptagged"# p3="pquoted" p4=pident\n',
        rawCfg,
    )
    node = doc.nodes[0]
    assert node.args == [
        kdl.IdentString("bare"),
        kdl.String("quoted"),
        kdl.RawString("raw"),
        kdl.RawString('a"#b'),
        kdl.RawString(""),
        kdl.RawString("tagged", tag="t"),
        kdl.String("quoted", tag="t"),
        kdl.IdentString("ident", tag="t"),
        kdl.RawString("line1\nline2"),
    ]
    assert node.props == {
        "p1": kdl.RawString("praw"),
        "p2": kdl.RawString("ptagged", tag="t"),
        "p3": kdl.String("pquoted"),
        "p4": kdl.IdentString("pident"),
    }

    # Default ("auto"): preserves RawString, picks ident/unescaped-quoted/raw/escaped-quoted otherwise
    assert doc.print() == (
        'n bare quoted #"raw"# ##"a"#b"## #""# (t)#"tagged"# (t)quoted (t)ident "line1\\nline2" '
        + 'p1=#"praw"# p2=(t)#"ptagged"# p3=pquoted p4=pident\n'
    )
    # Exact (True): IdentString -> ident, RawString -> raw, String -> quoted; node/prop/tag -> ident when possible
    assert doc.print(kdl.PrintConfig(respectStringType=True)) == (
        'n bare "quoted" #"raw"# ##"a"#b"## #""# (t)#"tagged"# (t)"quoted" (t)ident "line1\\nline2" '
        + 'p1=#"praw"# p2=(t)#"ptagged"# p3="pquoted" p4=pident\n'
    )
    # Plain (False): canonical ident-or-quoted everywhere, never raw
    assert doc.print(kdl.PrintConfig(respectStringType=False)) == (
        'n bare quoted raw "a\\"#b" "" (t)tagged (t)quoted (t)ident "line1\\nline2" '
        + "p1=praw p2=(t)ptagged p3=pquoted p4=pident\n"
    )

    # All 4 "auto" fallback steps on native Python str, node name, prop key, and tag:
    # 1. ident -> bare, 2. no escapes -> quoted, 3. escapes & raw-representable -> raw, 4. fallback -> escaped quoted
    autoDoc = kdl.Document(
        [
            kdl.Node(
                r"C:\node",
                tag='my"tag',
                args=["ident", "has space", r"C:\path", 'say "hi"', '"leading', "line1\nline2"],
                props={r"k\1": "v 1", "k2": 'a"#b', "k3": "a\x00b"},
            ),
        ],
    )
    assert autoDoc.print() == (
        '(#"my"tag"#)#"C:\\node"# ident "has space" #"C:\\path"# #"say "hi""# "\\"leading" "line1\\nline2" '
        + '#"k\\1"#="v 1" k2=##"a"#b"## k3="a\\u{0}b"\n'
    )
    # Under True, native str / node name / prop key / tag print as idents when possible, else quoted (never raw)
    assert autoDoc.print(kdl.PrintConfig(respectStringType=True)) == (
        '("my\\"tag")"C:\\\\node" ident "has space" "C:\\\\path" "say \\"hi\\"" "\\"leading" "line1\\nline2" '
        + '"k\\\\1"="v 1" k2="a\\"#b" k3="a\\u{0}b"\n'
    )
    # Under False, canonical ident-or-quoted everywhere
    assert autoDoc.print(kdl.PrintConfig(respectStringType=False)) == (
        '("my\\"tag")"C:\\\\node" ident "has space" "C:\\\\path" "say \\"hi\\"" "\\"leading" "line1\\nline2" '
        + '"k\\\\1"="v 1" k2="a\\"#b" k3="a\\u{0}b"\n'
    )
    # Round-trip under all 3 modes preserves all values identically
    modes: tuple[kdl.StringTypeMode, ...] = ("auto", True, False)
    for mode in modes:
        reparsed = kdl.parse(autoDoc.print(kdl.PrintConfig(respectStringType=mode)), kdl.ParseConfig(version=2))
        assert reparsed.nodes[0] == autoDoc.nodes[0]

    # Native str subclasses (e.g. StrEnum) and .to_kdl() returning str also print as idents when possible under True
    class MyStrEnum(enum.StrEnum):
        ALPHA = "alpha"

    class StrWrapper:
        def to_kdl(self) -> str:
            return "beta"

    exactCfg = kdl.PrintConfig(respectStringType=True)
    assert kdl.Node("n", args=[MyStrEnum.ALPHA, StrWrapper(), "gamma"]).print(exactCfg) == "n alpha beta gamma\n"

    # Tags on non-string Value subclasses respect respectStringType (ident under True/False, raw if escaped under auto)
    autoCfg = kdl.PrintConfig(respectStringType="auto")
    assert kdl.Binary(5, tag="t").print(exactCfg) == "(t)0b101"
    assert kdl.Octal(8, tag="t").print(exactCfg) == "(t)0o10"
    assert kdl.Hex(10, tag="t").print(exactCfg) == "(t)0xa"
    assert kdl.Decimal(1, tag="t").print(exactCfg) == "(t)1"
    assert kdl.Infinity(float("inf"), tag="t").print(exactCfg) == "(t)#inf"
    assert kdl.NaN(tag="t").print(exactCfg) == "(t)#nan"
    assert kdl.Bool(True, tag="t").print(exactCfg) == "(t)#true"
    assert kdl.Null(tag="t").print(exactCfg) == "(t)#null"
    assert kdl.ExactValue("42", tag="t").print(exactCfg) == "(t)42"
    assert kdl.Decimal(1, tag=r"a\b").print(autoCfg) == '(#"a\\b"#)1'
    assert kdl.Decimal(1, tag=r"a\b").print(exactCfg) == '("a\\\\b")1'

    # Invalid respectStringType raises ValueError
    badCfg = kdl.PrintConfig(respectStringType=t.cast("t.Any", "invalid"))
    for obj in (kdl.Document(), kdl.Node("n"), kdl.String("a"), kdl.IdentString("a"), kdl.RawString("a")):
        with pytest.raises(ValueError, match="Invalid respectStringType"):
            obj.print(badCfg)

    # Custom tags with default nativeUntaggedValues=True preserve IdentString vs String vs RawString
    taggedDoc = kdl.parse(
        'n (custom)#"raw"# (custom)"plain" (custom)bare k=(custom)pbare\n',
        kdl.ParseConfig(version=2),
    )
    assert taggedDoc.nodes[0].args == [
        kdl.RawString("raw", tag="custom"),
        kdl.String("plain", tag="custom"),
        kdl.IdentString("bare", tag="custom"),
    ]
    assert taggedDoc.nodes[0].props == {"k": kdl.IdentString("pbare", tag="custom")}

    # valueConverters can distinguish IdentString, String, RawString, and Stringish
    convCfg = kdl.ParseConfig(
        version=2,
        valueConverters={
            (..., kdl.RawString): lambda val, _pf: f"RAW:{val.value}",
            (..., kdl.IdentString): lambda val, _pf: f"IDENT:{val.value}",
            (..., kdl.String): lambda val, _pf: f"STR:{val.value}",
        },
    )
    convDoc = kdl.parse('n #"a"# "b" bare p=#"c"# q=d\n', convCfg)
    assert convDoc.nodes[0].args == ["RAW:a", "STR:b", "IDENT:bare"]
    assert convDoc.nodes[0].props == {"p": "RAW:c", "q": "IDENT:d"}


def test_stream_loc_line_endings() -> None:
    sLf = kdl_stream.Stream("ab\ncd\nef", kdl.ParseConfig(), startLine=10)
    assert sLf.loc(0) == (10, 1)
    assert sLf.loc(3) == (11, 1)
    assert sLf.loc(7) == (12, 2)

    sCr = kdl_stream.Stream("a\rb\rc", kdl.ParseConfig())
    assert [sCr.loc(i) for i in range(5)] == [(1, 1), (1, 2), (2, 1), (2, 2), (3, 1)]
    assert sCr.loc(-5) == (1, 1)

    sCrlf = kdl_stream.Stream("a\r\nb", kdl.ParseConfig())
    assert sCrlf.loc(0) == (1, 1)
    assert sCrlf.loc(3) == (2, 1)


# ---------------------------------------------------------------------------
# Deeply nested documents (arbitrary depth beyond Python recursion limit)
# ---------------------------------------------------------------------------


def _makeDeepDoc(depth: int, *, leaf: str = "leaf 1", slashdashPrefix: str = "") -> str:
    parts = [f"{slashdashPrefix}n{i} k={i} {{\n" for i in range(depth)]
    parts.append(f"{leaf}\n")
    parts.extend("}\n" for _ in range(depth))
    return "".join(parts)


def test_excessively_deep_kdl2_parse_and_print() -> None:
    depth = 2000
    src = _makeDeepDoc(depth, leaf='(end)leaf "ok" v=42')

    # Native parse + print (with indent="" and with non-empty indent past recursion limit)
    doc = kdl.parse(src, kdl.ParseConfig(version=2))
    cur = doc.nodes[0]
    for i in range(depth):
        assert cur.name == f"n{i}"
        assert cur.props == {"k": i}
        assert len(cur.nodes) == 1
        cur = cur.nodes[0]
    assert cur.name == "leaf"
    assert cur.tag == "end"
    assert cur.args == ["ok"]
    assert cur.props == {"v": 42}

    printedFlat = doc.print(kdl.PrintConfig(indent=""))
    reparsedFlat = kdl.parse(printedFlat, kdl.ParseConfig(version=2))
    assert reparsedFlat.print(kdl.PrintConfig(indent="")) == printedFlat

    # Also verify non-empty indentation and direct Node.print(indentLevel=...) past recursion limit
    printedIndented = doc.print(kdl.PrintConfig(indent=" "))
    assert (" " * depth) + "(end)leaf ok v=42\n" in printedIndented
    assert kdl.Node("deep").print(kdl.PrintConfig(indent=" "), indentLevel=1500) == (" " * 1500) + "deep\n"

    # Raw (nativeUntaggedValues=False) parse + print roundtrip at depth 2000
    rawCfg = kdl.ParseConfig(version=2, nativeUntaggedValues=False, nativeTaggedValues=False)
    rawDoc = kdl.parse(src, rawCfg)
    assert rawDoc.print(kdl.PrintConfig(indent="")) == _makeDeepDoc(depth, leaf="(end)leaf ok v=42")


def test_excessively_deep_kdl2_converters_slashdash_and_errors() -> None:
    depth = 1500

    # Node converters run bottom-up without recursion overflow
    seenOrder: list[str] = []

    def recordNode(node: kdl.Node, _pf: kdl.ParseFragment) -> kdl.Node:
        seenOrder.append(node.name)
        return node

    convDoc = kdl.parse(
        _makeDeepDoc(depth, leaf="leaf"),
        kdl.ParseConfig(version=2, nodeConverters={...: recordNode}),
    )
    assert len(convDoc.nodes) == 1
    assert len(seenOrder) == depth + 1
    assert seenOrder[0] == "leaf"
    assert seenOrder[-1] == "n0"

    # Deeply nested slashdashed node and slashdashed child blocks
    sdNodeDoc = kdl.parse(_makeDeepDoc(depth, slashdashPrefix="/- ") + "after 1\n", kdl.ParseConfig(version=2))
    assert [n.name for n in sdNodeDoc.nodes] == ["after"]

    sdBlocksParts = [f"n{i} /- {{\n" for i in range(depth)]
    sdBlocksParts.append("ignored 1\n")
    sdBlocksParts.extend("}\n" for _ in range(depth))
    sdBlocksDoc = kdl.parse("".join(sdBlocksParts), kdl.ParseConfig(version=2))
    assert len(sdBlocksDoc.nodes) == 1
    assert sdBlocksDoc.nodes[0].name == "n0"
    assert sdBlocksDoc.nodes[0].nodes == []

    # Unclosed deeply nested block raises ParseError (not RecursionError) at the unclosed `{`
    unclosed = "".join(f"n{i} {{\n" for i in range(depth)) + "leaf\n" + ("}\n" * (depth - 1))
    with pytest.raises(kdl.ParseError, match="Hit EOF while searching for end of child list") as excInfo:
        kdl.parse(unclosed, kdl.ParseConfig(version=2))
    assert excInfo.value.line == 1


def test_excessively_deep_kdl1_parse_and_print() -> None:
    depth = 2000
    src = _makeDeepDoc(depth, leaf="leaf true")

    for cfg in (kdl.ParseConfig(version=1), kdl.ParseConfig(version="auto")):
        doc = kdl.parse(src, cfg)
        cur = doc.nodes[0]
        for i in range(depth):
            assert cur.name == f"n{i}"
            cur = cur.nodes[0]
        assert cur.name == "leaf"
        assert cur.args == [True]
        printed = doc.print(kdl.PrintConfig(indent=""))
        assert "leaf #true\n" in printed
