# KDL-py

A handwritten Python 3.12+ implementation of a parser
for the [KDL Document Language](https://kdl.dev),
fully compliant with KDL 2.0.0,
with automatic fallback support for parsing legacy KDL 1.0.0 documents
(see [`ParseConfig.version`](#customizing-parsing)).
Printing always produces valid KDL 2.0.0.

[KDL](https://kdl.dev) is, as the name suggests, a document language,
filling approximately the same niche as JSON/YAML/XML/etc
to be a simple but powerful language for config files.
It combines the best of several of these languages,
while avoiding their pitfalls:
more general than JSON and more powerful than XML,
while avoiding the verbosity of XML
or the explosive complexity of YAML.

kdl-py, in particular, is focused on *ease-of-use*,
supporting things like date/times, ip addresses, urls, uuids, regexes, and binary data
directly in your KDL document
(via powerful but simple tagged values),
and parsing them into native Python types automatically,
or doing the reverse and letting you build KDL document trees
with these values directly,
and automatically and safely serializing them into KDL text
for later parsing!

You can customize parsing and serialization further for your application very easily,
by providing node or value "converters"
to turn plain KDL values or nodes
into application-specific types,
and then turning them back into KDL text automatically
merely by adding a `.to_kdl()` method to your classes.

**Contents:**
[Installing](#installing) ·
[Using](#using) ·
[Built-in Types & Tags](#built-in-types--tags) ·
[Customizing Parsing](#customizing-parsing) ·
[Customizing Printing](#customizing-printing) ·
[Full API Reference](#full-api-reference) ·
[`kdlreformat`](#kdlreformat)

## Installing

To use `kdl` as a Python library:

```sh
pip install kdl-py
```

Or, to install only the standalone `kdlreformat` command-line program globally:

```sh
pipx install kdl-py
```

Either installation makes the `kdlreformat` CLI available for canonicalizing KDL files.
See [below](#kdlreformat) for options.

## Using

The `kdl.parse(text: str, config: kdl.ParseConfig | None = None) -> kdl.Document` function parses, you guessed it, a string of KDL into a KDL document object:

```py3
>>> import kdl
>>> doc = kdl.parse('''
... node_name arg {
...     child_node foo=1 bar=#true
... }
... ''')
>>>
>>> doc
Document(
    nodes=[
        Node(
            name='node_name',
            tag=None,
            args=['arg'],
            props={},
            nodes=[
                Node(
                    name='child_node',
                    tag=None,
                    args=[],
                    props={
                        'foo': 1.0,
                        'bar': True
                    },
                    nodes=[]
                )
            ]
        )
    ],
    printConfig=None
)
```

> [!NOTE]
> Untagged decimal numbers (like `foo=1`) parse as Python `float` (`1.0`),
> since KDL's grammar does not distinguish integers from floats without a type annotation.
> Radix literals (`0x1a`, `0o755`, `0b101`) and integer-tagged numbers (like `(i64)1`) parse as `int`.

You can also create a `kdl.Parser()` object and call its `.parse()` method; `Parser` objects can set up parsing and printing options that'll apply by default. See [Customizing Parsing](#customizing-parsing) and [Customizing Printing](#customizing-printing) below.

If parsing fails, `kdl.parse()` raises a `kdl.ParseError` with 1-indexed `.line` and `.col` attributes and a formatted `.msg`.

### Accessing & Modifying Nodes

A `kdl.Document` is fully mutable.
In addition to raw `.nodes`, `.args`, and `.props` attributes,
documents and nodes provide convenient lookup and mutation shortcuts:

* Index children with `doc[key]` / `node[key]` (or `.get(key, default)` / `.getAll(key)`), using any [`NodeKey`](#nodekey).
* Call a node directly to access an argument by `int` index or a property by `str` key: `node(0)` or `node("foo")`.
* Use `node.getEntry(key, default)` for lookups with a fallback, and `node.setEntry(key, value)` to update/append entries.

```py3
>>> parent = doc["node_name"]
>>> parent(0)
'arg'
>>> child = parent["child_node"]
>>> child("foo")
1.0
>>> child.getEntry("missing", "fallback")
'fallback'
>>> child.setEntry("foo", 2)
>>> child.setEntry("baz", "hello world")
>>>
>>> print(doc)
node_name arg {
	child_node foo=2 bar=#true baz="hello world"
}
```

Stringifying a `kdl.Document` (or calling `doc.print(config)`) produces a valid KDL 2.0 document back.

## Built-in Types & Tags

By default (`nativeUntaggedValues=True` and `nativeTaggedValues=True`),
kdl-py automatically parses KDL values into native Python types and serializes those same Python types back into valid KDL 2.0.

### Untagged Primitives

| Python Type | Parsed From | Serializes As |
| --- | --- | --- |
| `bool` | `#true`, `#false` | `#true`, `#false` |
| `None` | `#null` | `#null` |
| `float` | untagged decimal numbers, `#inf`, `#-inf`, `#nan` | decimal number, `#inf`, `#-inf`, `#nan` |
| `int` | untagged `0x`/`0o`/`0b` literals (or integer tags below) | decimal number |
| `str` | untagged strings | formatted per [`PrintConfig.respectStringType`](#customizing-printing) (bare identifier, quoted, or raw string by default) |

### Reserved Tagged Types

All tags below are predefined by the [KDL specification](https://github.com/kdl-org/kdl/blob/main/SPEC.md#type-annotations):

| KDL Tag(s) | Input Kind | Python Type | Serializes As |
| --- | --- | --- | --- |
| `i8`, `i16`, `i32`, `i64`, `i128`, `u8`, `u16`, `u32`, `u64`, `u128`, `isize`, `usize` | number | `int` *(range- and integer-checked; `isize`/`usize` use platform width via `sys.maxsize`)* | untagged number |
| `f32`, `f64` | number | `float` | untagged number |
| `decimal64`, `decimal128` | number | `decimal.Decimal` | `(decimal)`-tagged string |
| `decimal` | string | `decimal.Decimal` | `(decimal)`-tagged string |
| `date-time`, `date`, `time` | string | `datetime.datetime`, `datetime.date`, `datetime.time` | `(date-time)`, `(date)`, `(time)`-tagged string |
| `ipv4`, `ipv6` | string | `ipaddress.IPv4Address`, `ipaddress.IPv6Address` | `(ipv4)`, `(ipv6)`-tagged string |
| `url`, `url-reference`, `irl`, `irl-reference` | string | `urllib.parse.ParseResult` | `(url)`-tagged string |
| `uuid` | string | `uuid.UUID` | `(uuid)`-tagged string |
| `regex` | string | `re.Pattern[str]` *(see note below)* | `(regex)`-tagged raw string (`#"..."#`) |
| `base64` | string | `bytes` (standard Base64) | `(base64)`-tagged string |
| `base85` | string | `bytes` (Ascii85, plain or `<~...~>`) | `(base64)`-tagged string |

> [!NOTE]
> When serializing a `re.Pattern`, it must be a `str` pattern (not `bytes`) and must not have non-default external flags
> (such as `re.IGNORECASE` passed to `re.compile()`);
> use inline flags in the pattern string (such as `(?i)`) instead, otherwise serialization raises a `ValueError`.

### Unsupported Reserved Tags

The remaining KDL reserved string annotations do not have an appropriate native type or validator
in the Python standard library, so kdl-py leaves them as tagged [`kdl.Stringish`](#full-api-reference) objects
(`kdl.IdentString`, `kdl.String`, or `kdl.RawString` depending on input syntax; you can handle them via custom `valueConverters` if needed):

* `duration`: Python's `datetime.timedelta` cannot represent ISO 8601 calendar years or months and has no ISO 8601 duration parser.
* `currency`, `country-2`, `country-3`, `country-subdivision`: no built-in ISO 4217 or ISO 3166 types or code tables.
* `email`, `idn-email`: no standard-library RFC 5322 / RFC 6531 email address value type.
* `hostname`, `idn-hostname`: no standard-library RFC 1123 / RFC 5890 hostname type.
* `url-template`: no standard-library RFC 6570 URI Template parser or type.

### Custom Types (`.to_kdl()`)

Any custom object with a `.to_kdl()` method can be inserted directly into a kdl-py document.
The method is called during stringification:
* When used as a value (in `node.args` or `node.props`), `.to_kdl()` must return a kdl-py `Value` or any of the supported native types above.
* When used in a node list (`doc.nodes` or `node.nodes`), `.to_kdl()` must return a `kdl.Node`.

See [Custom Converter & Serialization Example](#custom-converter--serialization-example) below for a complete round-trip example.

## Customizing Parsing

Parsing can be controlled via a `kdl.ParseConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `ParseConfig` object to `kdl.parse(text, config)`
	or `parser.parse(text, config)`
	(if you've constructed a `kdl.Parser`).
2. Creating a `kdl.Parser(parseConfig, printConfig)`,
	which automatically applies it to its `.parse()` method if not overridden.
3. Fiddling with the `kdl.parsing.defaults` object,
	which is used if nothing else provides a config.

A `ParseConfig` object has the following properties:

* `nativeUntaggedValues: bool = True`

	Controls whether the parser produces native Python objects (`str`, `float` for decimals/specials, `int` for `0x`/`0o`/`0b`, `bool`, `None`)
	when parsing untagged values (those without a `(tag)` prefix),
	or always produces kdl-py `Value` objects (`kdl.IdentString`, `kdl.String`, `kdl.RawString`, `kdl.Decimal`, `kdl.Hex`, `kdl.Infinity`, `kdl.NaN`, etc.).

* `nativeTaggedValues: bool = True`

	Controls whether the parser converts KDL's reserved tagged values into native Python types
	(see [Reserved Tagged Types](#reserved-tagged-types) above).
	When `False`, tagged values are left as tagged kdl-py `Value` objects unless matched by a custom converter.

* `valueConverters: dict[ValueKey, Callable[[Value, ParseFragment], Any]] = {}`

	A dictionary of `ValueKey -> converter` functions,
	letting you parse values (like `(color)"#ff00aa"`) into whatever types you'd like.

	Whenever the parser encounters a value,
	it searches this dict in insertion order for a [`ValueKey`](#valuekey) matching the value.
	When one matches, it calls the converter with two arguments:
	the constructed kdl-py `Value` object,
	and a [`ParseFragment`](#parsefragment) giving access to the raw input characters
	(for the value itself, excluding the tag).
	Whatever the converter returns is inserted into the document in place of the value.

	You can return a kdl-py `Value` (e.g. `kdl.Decimal` or `kdl.ExactValue`),
	a built-in Python type,
	or a custom object (give it a `.to_kdl()` method so it can serialize back out).

	Only *one* conversion happens per value:
	your converters are tried in insertion order,
	and if none apply, `nativeUntaggedValues` / `nativeTaggedValues` runs (if enabled).
	Returning `NotImplemented` from a converter skips it and continues searching subsequent converters (and built-in conversions).

* `nodeConverters: dict[NodeKey, Callable[[Node, ParseFragment], Any]] = {}`

	Similar to `valueConverters`,
	except it uses [`NodeKey`s](#nodekey) and converts `kdl.Node`s instead
	(children are converted bottom-up before their parent).

	There is no built-in conversion of nodes;
	if no converter applies (or all matching converters return `NotImplemented`),
	the `kdl.Node` is kept as-is.

* `version: Literal[1, 2, "auto"] = "auto"`

	Controls which KDL grammar version to parse with.
	By default (`"auto"`), the parser parses as KDL 2.0,
	respecting any leading `/- kdl-version 1` or `/- kdl-version 2` version marker,
	and opportunistically falls back to KDL 1.0
	if a KDL 2.0 parse error looks like a KDL 1.0 construct
	(such as unprefixed `true`/`false`/`null` keywords, `r"..."`/`r#"..."#` raw strings,
	multiline single-quoted strings, `\/` escapes, or KDL 1.0 bare identifiers).
	A leading `/- kdl-version 2` marker disables KDL 1.0 fallback;
	if fallback is attempted and also fails, the original KDL 2.0 `ParseError` is raised.

	Set `version=2` for strict KDL 2.0 parsing,
	or `version=1` to parse strictly as KDL 1.0
	(an explicit `1` or `2` overrides any `/- kdl-version` marker in the document;
	any other value raises `ValueError`).
	KDL 1.0 documents parse into the exact same `Document`/`Node`/`Value` structure,
	respecting all the same `ParseConfig` options and converters,
	and serialize back out as valid KDL 2.0.

### `ParseFragment`

`kdl.ParseFragment` is passed as the second argument to custom `valueConverters` and `nodeConverters`,
giving direct access to the original input characters before any processing
(useful, for example, for numeric types that would lose precision as a `float`):

* `pf.fragment: str`: raw source slice for the value (after the tag, if any) in value converters,
	or for the node's tag (if any) and name in node converters.
* `pf.error(msg: str) -> kdl.ParseError`: builds a `kdl.ParseError` already pointing at the fragment's source location,
	ready to `raise` so converter failures match native parse errors.

### Custom Converter & Serialization Example

```py3
>>> from dataclasses import dataclass
>>> import kdl
>>>
>>> @dataclass
... class Color:
...     r: int
...     g: int
...     b: int
...     def to_kdl(self) -> kdl.String:
...         return kdl.String(f"#{self.r:02x}{self.g:02x}{self.b:02x}", tag="color")
...
>>> def parse_color(val: kdl.Value, pf: kdl.ParseFragment) -> Color:
...     if not isinstance(val, kdl.Stringish):
...         return NotImplemented
...     s = val.value.lstrip("#")
...     if len(s) != 6:
...         raise pf.error(f"Expected 6-digit hex color, got {pf.fragment}")
...     return Color(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
...
>>> parser = kdl.Parser(
...     kdl.ParseConfig(valueConverters={"color": parse_color}),
...     kdl.PrintConfig(indent="  "),
... )
>>> doc = parser.parse('theme {\n  accent (color)"#ff8800"\n}\n')
>>> doc["theme"]["accent"](0)
Color(r=255, g=136, b=0)
>>> print(doc)
theme {
  accent (color)"#ff8800"
}
```

## Customizing Printing

Like parsing, printing a kdl-py `Document` back to a KDL 2.0 string can be controlled by a `kdl.PrintConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `PrintConfig` object to `doc.print(config)`.
2. Setting `doc.printConfig` to a `PrintConfig`.
	(This is done automatically for any documents produced by a `Parser`,
	if you pass the `printConfig` option to the constructor.)
3. Fiddling with the `kdl.printing.defaults` object,
	which is used if nothing else provides a config.

```py3
>>> doc = kdl.parse('server port=8080 host="localhost" {\n  route "/api"\n}\n')
>>> print(doc.print(kdl.PrintConfig(indent="  ", sortProperties=True)))
server host=localhost port=8080.0 {
  route "/api"
}
```

A `PrintConfig` object has the following properties:

* `indent: str = "\t"`

	The string used for each indent level.
	Defaults to tabs,
	but can be set to a sequence of spaces if desired
	(or anything else).

* `semicolons: bool = False`

	Whether or not nodes are ended with semicolons.
	(The printer always ends nodes with a newline anyway,
	so this is purely a stylistic choice.)

* `printNullArgs: bool = True`

	When `False`, automatically skips over any `#null`/`None` arguments.
	This will corrupt documents that use the `#null` keyword intentionally,
	but can be useful if you'd prefer to use a `None` value
	as a signal that the argument has been removed.

* `printNullProps: bool = True`

	Identical to `printNullArgs`,
	but applies to properties rather than arguments.

* `respectStringType: bool | Literal["auto"] = "auto"`

	Controls how string values (as well as native Python `str` values, node names, property keys, and tags)
	are formatted in KDL 2.0 output:

	* `"auto"` (default): preserves `kdl.RawString` values as raw strings
		(falling back to escaped quoted strings if the value can't be printed as a single-line raw string).
		For all other strings (`kdl.IdentString`, `kdl.String`, native `str`, node names, property keys, and tags),
		picks the cleanest representation in order:
		1. bare identifier string if valid in KDL 2.0,
		2. normal quoted string if it contains no characters requiring escapes,
		3. single-line raw string (`#"..."#`) if representable without escapes,
		4. quoted string with escapes as a final fallback.
	* `True`: preserves each string's exact type as much as reasonably possible:
		* `kdl.IdentString` (as well as native Python `str` values, node names, property keys, and tags) outputs as a bare identifier when valid in KDL 2.0, falling back to a quoted string otherwise
		* `kdl.RawString` outputs as a raw string (falling back to quoted if manually constructed to not be a valid single-line raw string)
		* `kdl.String` always outputs as a normal quoted string
	* `False`: ignores input string types and always prints bare identifiers when valid in KDL 2.0,
		or normal quoted strings otherwise (never raw strings).

	> [!NOTE]
	> If using the default `nativeUntaggedValues=True` ParseConfig option,
	> all strings become native Python `str` objects,
	> so the type can't be exactly preserved.

* `respectRadix: bool = True`

	Similar to `respectStringType`,
	when `True` the printer will output numbers as the radix they were in the input,
	like `0x1b` for hex numbers.
	When `False`, the printer always outputs decimal numbers.

	Again, this only has an effect on kdl-py objects;
	native Python numbers are printed as normal for Python.

* `exponent: str = "e"`

	What character to use for the exponent part of decimal numbers,
	when printed with scientific notation.
	Should only be set to `"e"` or `"E"`.

	Like the previous options, this only has an effect on kdl-py objects;
	native Python numbers are printed as normal for Python.

* `sortProperties: bool = False`

	When `False`, a node's properties are printed in insertion order.
	When `True`, properties are printed sorted lexicographically by key.

## Full API Reference

* `kdl.parse(input: str, config: kdl.ParseConfig | None = None) -> kdl.Document`
* `kdl.Parser(parseConfig: kdl.ParseConfig | None = None, printConfig: kdl.PrintConfig | None = None)`
	* `parser.parse(chars: str, config: kdl.ParseConfig | None = None) -> kdl.Document`
* `kdl.Document(nodes: list[kdl.Node] = [], printConfig: kdl.PrintConfig | None = None)`
	* `doc.print(config: kdl.PrintConfig | None = None) -> str`
	* `doc[key: kdl.NodeKey] -> kdl.Node` returns the first child node matching the [`NodeKey`](#nodekey). Raises a `KeyError` if nothing matches, similar to a `dict`.
	* `doc.get(key: kdl.NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#nodekey), or `default` if nothing matches.
	* `doc.getAll(key: kdl.NodeKey) -> Iterable[kdl.Node]` returns an iterator of all child nodes matching the [`NodeKey`](#nodekey).
* `kdl.Node(name: str, tag: str | None = None, args: list[Any] = [], props: dict[str, Any] = {}, nodes: list[kdl.Node] = [])`
	* `node.print(config: kdl.PrintConfig | None = None, indentLevel: int = 0) -> str`
	* `node[key: kdl.NodeKey] -> kdl.Node` returns the first child node matching the [`NodeKey`](#nodekey). Raises a `KeyError` if nothing matches, similar to a `dict`.
	* `node.get(key: kdl.NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#nodekey), or `default` if nothing matches.
	* `node.getAll(key: kdl.NodeKey) -> Iterable[kdl.Node]` returns an iterator of all child nodes matching the [`NodeKey`](#nodekey).
	* `node.matchesKey(key: kdl.NodeKey) -> bool` returns whether the node matches the [`NodeKey`](#nodekey).
	* `node(key: int | str) -> Any`:
		callable shorthand for `node.getEntry(key)` without a default (e.g. `node(0)` or `node("prop")`).
		Gets an argument by `int` index (from `node.args`) or a property by `str` key (from `node.props`),
		raising `IndexError` / `KeyError` if missing, or `TypeError` for `bool` or other non-`int`/`str` keys.
	* `node.getEntry(key: int | str) -> Any` / `node.getEntry(key: int | str, default: Any) -> Any`:
		like `node(key)`, but accepts an optional `default` value returned when the index/key is missing
		(an existing `#null`/`None` entry still returns `None`, not `default`).
	* `node.setEntry(key: int | str, value: Any) -> None`:
		sets an argument by `int` index (appending if `key == len(node.args)`, raising `IndexError` if out of range)
		or sets/inserts a property by `str` key.
		Passing a `bool` or other non-`int`/`str` key raises `TypeError`.
	* `node.getProps(key: kdl.ValueKey) -> Iterable[tuple[str, Any]]` returns an iterator of `(name, value)` pairs for the properties whose value matches the [`ValueKey`](#valuekey).
		(Note: non-KDL values such as `int` have no tag and match `None`/`...` tag keys and `TypeKey` checks.)
	* `node.getArgs(key: kdl.ValueKey) -> Iterable[Any]` returns an iterator of the arguments matching the [`ValueKey`](#valuekey).
		(Same note as `.getProps()`.)

* `kdl.Value` ‡
	* `val.print(config: kdl.PrintConfig | None = None) -> str`
	* `val.matchesKey(key: kdl.ValueKey) -> bool` returns whether the value matches the [`ValueKey`](#valuekey).
* `kdl.Binary(value: int, tag: str | None = None)`
* `kdl.Octal(value: int, tag: str | None = None)`
* `kdl.Decimal(mantissa: int | float, exponent: int = 0, tag: str | None = None)`
	* `dec.value`: readonly `float`, `float(mantissa)` when `exponent == 0` or parsed from `f"{mantissa}e{exponent}"`
* `kdl.Hex(value: int, tag: str | None = None)`
* `kdl.Infinity(value: float, tag: str | None = None)`
* `kdl.NaN(value: float = float("nan"), tag: str | None = None)`
* `kdl.Bool(value: bool, tag: str | None = None)`
* `kdl.Null(tag: str | None = None)`
	* `null.value`: readonly, always `None`
* `kdl.IdentString(value: str, tag: str | None = None)`: bare identifier string (e.g. `foo`)
* `kdl.RawString(value: str, tag: str | None = None)`: raw string (e.g. `#"foo"#`)
* `kdl.String(value: str, tag: str | None = None)`: quoted string (e.g. `"foo"`)
* `kdl.ExactValue(chars: str, tag: str | None = None)` †: emits `chars` verbatim (with optional tag); `exact.value` returns `chars`
* `kdl.Numberish`, `kdl.Stringish` ‡
* `kdl.ParseConfig(...)`: see [Customizing Parsing](#customizing-parsing)
	* `kdl.parsing.defaults`: default `ParseConfig`
* `kdl.PrintConfig(...)`: see [Customizing Printing](#customizing-printing)
	* `kdl.printing.defaults`: default `PrintConfig`
* `kdl.ParseError`: raised for all parsing errors
	* `error.msg: str`: formatted error message with location
	* `error.line: int`: 1-indexed line number
	* `error.col: int`: 1-indexed column number
* `kdl.ParseFragment`: passed to converter functions
	* `pf.fragment: str`: slice from the source string
	* `pf.error(msg: str) -> kdl.ParseError`: returns a `kdl.ParseError` with error location set
* `kdl.nodeMatchesKey(node: Any, key: kdl.NodeKey) -> bool`
* `kdl.valueMatchesKey(value: Any, key: kdl.ValueKey) -> bool`
* `kdl.tagMatchesKey(val: str | None, key: kdl.TagKey) -> bool`
* `kdl.nameMatchesKey(val: str | None, key: kdl.NameKey) -> bool`
* `kdl.typeMatchesKey(val: Any, key: kdl.TypeKey) -> bool`
	* Functions implementing the tag/name/type matching
		used by `node.matchesKey()` and `value.matchesKey()`,
		in case you want to use the same filtering yourself.

† Not produced by the parser.
Can be returned by a user's `.to_kdl()` method
if they want to produce a value *precisely* in a particular syntax,
in a way that the built-in kdl-py classes don't.

‡ Not produced by the parser.
These are base classes to help in type testing:
`Value` matches all value classes,
`Numberish` matches all six numeric value classes (`Binary`, `Octal`, `Decimal`, `Hex`, `Infinity`, `NaN`),
and `Stringish` matches all three string value classes (`IdentString`, `String`, `RawString`).

A few type aliases also exist,
used by the module in a few places
and potentially useful for your code:

* `kdl.KDLAny`: a `Document`, `Node`, or any of the `Value` subtypes
* `kdl.KDLValue`: any of the `Value` subtypes
* `kdl.KDLishValue`: a `KDLValue` or one of the supported Python native types (see [Built-in Types & Tags](#built-in-types--tags))
* `kdl.ParseVersion`: `Literal[1, 2, "auto"]`, the type for `ParseConfig.version`
* `kdl.StringTypeMode`: `bool | Literal["auto"]`, the type for `PrintConfig.respectStringType`
* `kdl.ValueKey`, `kdl.NodeKey`, `kdl.TagKey`, `kdl.NameKey`, `kdl.TypeKey`:
	the types for [`ValueKey`s](#valuekey) and [`NodeKey`s](#nodekey)
	(and their individual pieces)

These aliases only exist when `typing.TYPE_CHECKING` is true,
so they're *only* useful for writing types;
they won't be visible at runtime.

### `NodeKey`

A few data structures and functions take a `NodeKey`
to match against a node,
based on its name and/or tag.

A `NodeKey` is either a `NameKey` or a `(TagKey, NameKey)` tuple,
matching against the node's name and/or tag.

`NameKey`s and `TagKey`s can be:

* `None`:
	When matched against a tag, only succeeds against a `None` tag
	(aka a tagless node like `foo`).
	When matched against a name, automatically succeeds,
	since nodes are guaranteed to have a name.
* `...`:
	Always succeeds.
	Use this when, say,
	you want to match against a particular tag, regardless of the nodename,
	like `("my-tag", ...)`.
* a `str`:
	Succeeds if the tag/name is that exact string.
* a `re.Pattern` (a regex, such as `re.compile(r".*foo")`):
	Succeeds if the regex matches the tag/name.
	Note that this uses `.match()` semantics,
	automatically anchoring against the start of the string;
	prepend your regex with `.*?` if you want it to match anywhere.
* a function, taking a `str | None` and returning a `bool`:
	Succeeds if the function,
	when called with the tag/name,
	returns `True`.

So, for example,
`doc.getAll("foo")` returns all nodes whose name is `"foo"`, regardless of tag;
`doc.getAll(("my-tag", ...))` returns all nodes whose tag is `"my-tag"`, regardless of node name;
`doc.getAll(re.compile(r"-"))` returns all nodes whose name starts with `"-"`.

### `ValueKey`

Similarly to `NodeKey`, a few things take a `ValueKey`
to match against values,
based on its tag and/or type.

A `ValueKey` is either a `TagKey` or a `(TagKey, TypeKey)` tuple,
matching against the value's tag and/or type.

`TagKey` works identically to how it appears in [`NodeKey`](#nodekey).
`TypeKey` is either `...`, which matches any type,
or the same sort of argument you'd pass as the second argument to `isinstance()`.

For example,
a `"foo"` key matches any value with the tag `"foo"`, like `(foo)1` or `(foo)"bar"`;
a `(..., kdl.Numberish)` key matches any number value (`Hex`, `Octal`, `Binary`, `Decimal`, `Infinity`, or `NaN`), regardless of tag.


## `kdlreformat`

The `kdlreformat` command-line program is installed by default
when you install this module from PyPI.
It can also be run manually from the `kdlreformat.py` file
at the root of this repository
(or from the `kdl.cli.cli()` function).
Because it uses the default `version="auto"` parser and always prints KDL 2.0,
it can also be used to upgrade KDL 1.0 files to canonical KDL 2.0.

```sh
# Reformat stdin to stdout with 2-space indentation and sorted properties
cat config.kdl | kdlreformat --indent 2 --sort-properties

# Reformat a file in-place (input is parsed before writing, so parse errors won't truncate it)
kdlreformat config.kdl config.kdl

# Upgrade a KDL 1.0 document to KDL 2.0
kdlreformat --version 1 old.kdl new.kdl
```

```
usage: kdlreformat [-h] [--indent INDENT] [--semicolons] [--radix]
                   [--no-radix] [--string-type {auto,exact,plain}]
                   [--raw-strings] [--no-raw-strings] [--exponent EXPONENT]
                   [--sort-properties] [--version {1,2,auto}]
                   [infile] [outfile]

KDL parser/printer, letting you easily reformat KDL files into a canonical
representation.

positional arguments:
  infile
  outfile

options:
  -h, --help            show this help message and exit
  --indent INDENT       How many spaces for each level of indent. -1 indicates
                        to indent with tabs.
  --semicolons          Whether to end nodes with semicolons or not.
  --radix               Output numeric values in the radix used by the input.
                        (0x1a outputs as 0x1a)
  --no-radix            Convert all numeric arguments to decimal. (0x1a
                        outputs as 26)
  --string-type {auto,exact,plain}
                        How to format strings: 'auto' (preserve raw strings,
                        pick cleanest form otherwise; default), 'exact'
                        (preserve input ident/quoted/raw string types), or
                        'plain' (always output ident or quoted strings, never
                        raw).
  --raw-strings         Alias for --string-type=exact.
  --no-raw-strings      Alias for --string-type=plain.
  --exponent EXPONENT   What character to use ('e' or 'E') for indicating
                        exponents on scinot numbers.
  --sort-properties     Sort node properties lexicographically by key instead
                        of preserving insertion order.
  --version {1,2,auto}  KDL grammar version to parse ('1', '2', or 'auto').
                        Default is 'auto'.
```
