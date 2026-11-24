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

## Installing

```sh
pipx install kdl-py
```

When installed, a `kdlreformat` command-line program is also made available,
which can canonicalize a KDL document. See [below](#kdlreformat) for options.

## Using

The `kdl.parse(str, parseConfig|None)` function parses, you guessed it, a string of KDL into a KDL document object:

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

You can also create a `kdl.Parser()` object and call its `.parse()` method; `Parser` objects can set up parsing and printing options that'll apply by default. See below for how to configure parsing options.

Either way, you'll get back a `kdl.Document` object, which is fully mutable. By default, untagged KDL values are represented with native Python objects.

```py3
>>> doc.nodes[0].nodes[0].props["foo"] = 2
>>>
>>> print(doc)
node_name arg {
	child_node foo=2 bar=#true
}

```

Stringifying a `kdl.Document` object will produce a valid KDL 2.0 document back. You can also call `doc.print(printConfig|None)` to customize the printing with a `PrintConfig` object, described below.

### Inserting Native Values

kdl-py allows a number of native Python objects to be used directly in KDL documents by default,
and allows you to customize your own objects for use.

kdl-py automatically recognizes and correctly serializes the following objects:

* `bool`: as untagged `#true` or `#false`
* `None`: as untagged `#null`
* `int`, `float`: as untagged decimal number (or `#inf`, `#-inf`, `#nan` for non-finite floats)
* `str`: as untagged string (bare identifier when valid in KDL 2.0, otherwise quoted string)
* `bytes`: as `(base64)`-tagged string
* `decimal.Decimal`: as `(decimal)`-tagged string
* `datetime`, `date`, and `time`: as `(date-time)`, `(date)`, or `(time)`-tagged strings
* `ipaddress.IPv4Address` and `ipaddress.IPv6Address`: as `(ipv4)` or `(ipv6)`-tagged strings
* `urllib.parse.ParseResult` (the result of calling `urllib.parse.urlparse()`): as `(url)`-tagged string
* `uuid.UUID`: as `(uuid)`-tagged string
* `re.Pattern` (the result of calling `re.compile()` on a `str`): as `(regex)`-tagged raw string (`#"..."#`).
	Must not have non-default external flags (such as `re.IGNORECASE` passed to `re.compile()`);
	use inline flags in the pattern string (such as `(?i)`) instead, otherwise serialization raises a `ValueError`.

All of the tags used above are reserved and predefined by the [KDL specification](https://github.com/kdl-org/kdl/blob/main/SPEC.md#type-annotations).

In addition, any object with a `.to_kdl()` method
can be used in a kdl-py document.
The method will be called when the document is stringified:
when used as a value, it must return one of the kdl-py `Value` types
or any of the native types defined above;
when used in a node list (`doc.nodes` or `node.nodes`), it must return a `kdl.Node`.

(For *parsing* KDL into these native types,
or your own types,
see the `ParseConfig` section, below.)

## Customizing Parsing

Parsing can be controlled via a `kdl.ParseConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `ParseConfig` object to `kdl.parse(str, ParseConfig|None)`
	or `parser.parse(str, ParseConfig|None)`
	(if you've constructed a `kdl.Parser`).
2. Creating a `kdl.Parser(parseConfig|None, printConfig|None)`,
	which automatically applies it to its `.parse()` method if not overridden.
3. Fiddling with the `kdl.parsing.defaults` object,
	which is used if nothing else provides a config.

A `ParseConfig` object has the following properties:

* `nativeUntaggedValues: bool = True`

	Controls whether the parser produces native Python objects (`str`, `int`, `float` including `inf`/`-inf`/`nan`, `bool`, `None`) when parsing untagged values (those without a `(foo)` prefix), or always produces kdl-py objects (such as `kdl.String`, `kdl.Decimal`, `kdl.Infinity`, `kdl.NaN`, etc).

* `nativeTaggedValues: bool = True`

	Controls whether the parser produces native Python objects
	when parsing *tagged* values,
	for [KDL's reserved type annotations](https://github.com/kdl-org/kdl/blob/main/SPEC.md#type-annotations):

	* `i8`, `i16`, `i32`, `i64`, `i128`, `u8`, `u16`, `u32`, `u64`, `u128`, `isize`, `usize` on numbers:
		Checks that the value is an integer in the specified range
		(`isize`/`usize` use the current Python platform's pointer width via `sys.maxsize`),
		raising `ParseError` on out-of-range or fractional values,
		then converts it to an `int`.
		(It will serialize back out as an ordinary untagged number.)
	* `f32`, `f64` on numbers:
		Converts it to a `float`.
		(It will serialize back out as an ordinary untagged number.)
	* `decimal64`, `decimal128` on numbers, and `decimal` on strings:
		Converts it to a `decimal.Decimal` object.
		(Always reserializes to a `(decimal)`-tagged string.)
	* `date-time`, `date`, `time` on strings:
		Converts to a `datetime`, `date`, or `time` object.
	* `ipv4`, `ipv6` on strings:
		Converts it to an `ipaddress.IPv4Address` or `ipaddress.IPv6Address` object.
	* `url`, `url-reference`, `irl`, `irl-reference` on strings:
		Converts it to a `urllib.parse.ParseResult` tuple.
		(Always reserializes to a `(url)`-tagged string.)
	* `uuid` on strings:
		Converts it to a `uuid.UUID` object.
	* `regex` on strings:
		Converts it to a `re.Pattern` object.
		(It will serialize back out as a `(regex)`-tagged raw string.)
	* `base64` on strings:
		Converts it to a `bytes` object using standard Base64 decoding.
	* `base85` on strings:
		Converts it to a `bytes` object using Ascii85 decoding (both plain and Adobe `<~...~>`-delimited).
		(Reserializes to a `(base64)`-tagged string.)

	The remaining KDL reserved string annotations do not have an appropriate native type or validator
	in the Python standard library, so kdl-py leaves them as tagged `kdl.String` objects
	(you can handle them via custom `valueConverters` if needed):

	* `duration`: Python's `datetime.timedelta` cannot represent ISO 8601 calendar years or months and has no ISO 8601 duration parser.
	* `currency`, `country-2`, `country-3`, `country-subdivision`: no built-in ISO 4217 or ISO 3166 types or code tables.
	* `email`, `idn-email`: no standard-library RFC 5322 / RFC 6531 email address value type.
	* `hostname`, `idn-hostname`: no standard-library RFC 1123 / RFC 5890 hostname type.
	* `url-template`: no standard-library RFC 6570 URI Template parser or type.


* `valueConverters: dict[ValueKey, Callable] = {}`

	A dictionary of ValueKey->converter functions,
	letting you parse values
	(like `(date)"2021-01-01"`)
	into whatever types you'd like.

	Whenever the parser encounters a value,
	it will attempt to find an entry in this dict
	whose [`ValueKey`](#valuekey) matches the value.
	If it succeeds,
	it will call the associated converter function with two arguments:
	the fully-constructed kdl-py object,
	and a `ParseFragment` object giving you access
	to the precise characters parsed from the document
	(for the value; the tag, if any, won't be included, as you can get it from the object itself).
	Whatever you return will be inserted into the document
	instead of the originally-parsed value.

	You can produce KDL values
	(such as parsing `(hex)"0x12.e5"` into a `kdl.Decimal`,
	since KDL doesn't support fractional hex values),
	or produce any other Python type.
	If you return a non-KDL type,
	you probably want to ensure it has a `.to_kdl()` method
	(or is one of the supported built-in types),
	so it can be serialized back into a KDL document.

	Note that only *one* conversion can happen to a given value.
	Your converters are checked
	in the dictionary's iteration order,
	then if none of them succeeded
	it will attempt to run the `nativeUntaggedValues` or `nativeTaggedValues` behaviors,
	if they're turned on.
	If a converter returns `NotImplemented`,
	it will continue looking for a matching converter.


* `nodeConverters: dict[NodeKey, Callable] = {}`

	Similar to `valueConverters`,
	except it uses [`NodeKey`s](#nodekey),
	and it converts `kdl.Node`s instead.

	There is no native conversion of nodes;
	if none of your converters successfully run,
	the node will be inserted as-is.


* `version: Literal[1, 2, "auto"] = "auto"`

	Controls which KDL grammar version to parse with.
	By default (`"auto"`), the parser parses as KDL 2.0,
	respecting any leading `/- kdl-version 1` or `/- kdl-version 2` version marker,
	and opportunistically falls back to KDL 1.0 (via `kdl.kdl1`)
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


### ParseFragment

`kdl.ParseFragment` is passed to your custom converters,
specified in `kdl.ParseConfig.valueConverters` and `kdl.ParseConfig.nodeConverters`,
giving you direct access to the input characters
before any additional processing was done on them.
This is useful, for example,
to handle numeric types
that might have lost precision in the normal parse.

It exposes a `.fragment` property,
containing the raw text of the value
(after the tag, if any) for value converters,
or the raw text of the node's tag (if any) and name for node converters.

It also exposes a `.error(str)` method,
which takes a custom error message
and returns a `kdl.ParseError`
with the `ParseFragment`'s location already built in,
ready for you to `raise`.
This should be used if your conversion fails for any reason,
so your errors look the same as native parse errors.

## Customizing Printing

Like parsing, printing a kdl-py `Document` back to a KDL 2.0 string can be controlled by a `kdl.PrintConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `PrintConfig` object to `doc.print(PrintConfig|None)`.
2. Setting `doc.printConfig` to a `PrintConfig`.
	(This is done automatically for any documents produced by a `Parser`,
	if you pass the `printConfig` option to the constructor.)
3. Fiddling with the `kdl.printing.defaults` object,
	which is used if nothing else provides a config.

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
	Should only be set to "e" or "E".

	Like the previous options, this only has an effect on kdl-py objects;
	native Python numbers are printed as normal for Python.

* `sortProperties: bool = False`

	When `False`, a node's properties are printed in insertion order.
	When `True`, properties are printed sorted lexicographically by key.

## Full API Reference

* `kdl.parse(str, config: kdl.ParseConfig|None) -> kdl.Document`
* `kdl.Parser(parseConfig: kdl.ParseConfig|None, printConfig: kdl.PrintConfig|None)`
	* `parser.parse(str, config: kdl.ParseConfig|None) -> kdl.Document`
* `kdl.Document(nodes: list[kdl.Node]?, printConfig: kdl.PrintConfig|None)`
	* `doc.print(PrintConfig|None) -> str`
	* `doc[NodeKey] -> Node` returns the first child node matching the [`NodeKey`](#nodekey). Raises a `KeyError` if nothing matches the `NodeKey`, similar to a `dict`.
	* `doc.get(NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#nodekey). Returns the default value if nothing matches.
	* `doc.getAll(NodeKey) -> Iterable[kdl.Node]` returns all child nodes matching the [`NodeKey`](#nodekey)
* `kdl.Node(name: str, tag: str|None = None, args: list[Any]?, props: dict[str, Any]?, nodes: list[kdl.Node]?)`
	* `node.print(PrintConfig|None = None, indentLevel: int = 0) -> str`
	* `node[NodeKey] -> Node` returns the first child node matching the [`NodeKey`](#nodekey). Raises a `KeyError` if nothing matches the `NodeKey`, similar to a `dict`.
	* `node.get(NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#nodekey). Returns the default value if nothing matches.
	* `node.getAll(NodeKey) -> Iterable[kdl.Node]` returns all child nodes matching the [`NodeKey`](#nodekey)
	* `node.matchesKey(NodeKey) -> bool` returns whether the node matches the [`NodeKey`](#nodekey)
	* `node(key: int|str) -> Any` / `node.getEntry(key: int|str, default = UNSET) -> Any`:
		(Note: that is, the node is callable directly, as `node(0)` or `node("prop")`.)
		Gets an argument by `int` index (from `node.args`) or a property by `str` key (from `node.props`).
		If the index/key is missing, returns `default` when provided (in `.getEntry()`, only), 
		or raises `IndexError` / `KeyError` otherwise.
		Passing a `bool` or other non-`int`/`str` key raises `TypeError`.
	* `node.setEntry(key: int|str, value: Any) -> None`:
		sets an argument by `int` index (appending if `key == len(node.args)`, raising `IndexError` if out of range)
		or sets/inserts a property by `str` key.
		Passing a `bool` or other non-`int`/`str` key raises `TypeError`.
	* `node.getProps(ValueKey) -> Iterable[tuple[str, Any]]` returns an iterator of `(name, value)` pairs for the properties whose value matches the [`ValueKey`](#valuekey)
		(Note: for this purpose, non-KDL values, such as `int`, have no tag, and can only meaningfully be tested via a `TypeKey`.)
	* `node.getArgs(ValueKey) -> Iterable[Any]` returns an iterator of the arguments that match the [`ValueKey`](#valuekey)
		(Same disclaimer as `.getProps()`.)

* `kdl.Value` ‡
	* `val.print(PrintConfig|None) -> str`
	* `val.matchesKey(ValueKey) -> bool` returns whether the value matches the [`ValueKey`](#valuekey)
* `kdl.Binary(value: int, tag: str|None = None)`
* `kdl.Octal(value: int, tag: str|None = None)`
* `kdl.Decimal(mantissa: int|float, exponent: int = 0, tag: str|None = None)`
	* `dec.value`: readonly `float`, `float(mantissa)` when `exponent == 0` or parsed from `f"{mantissa}e{exponent}"`
* `kdl.Hex(value: int, tag: str|None = None)`
* `kdl.Infinity(value: float, tag: str|None = None)`
* `kdl.NaN(value: float = float("nan"), tag: str|None = None)`
* `kdl.Bool(value: bool, tag: str|None = None)`
* `kdl.Null(tag: str|None = None)`
	* `null.value`: readonly, always `None`
* `kdl.IdentString(value: str, tag: str|None = None)`
* `kdl.RawString(value: str, tag: str|None = None)`
* `kdl.String(value: str, tag: str|None = None)`
* `kdl.ExactValue(chars: str, tag: str|None = None)` †
* `kdl.Numberish`, `kdl.Stringish` ‡
* `kdl.ParseConfig(...)` see above for options
	* `kdl.parsing.defaults`: default `ParseConfig`
* `kdl.PrintConfig(...)` see above for options
	* `kdl.printing.defaults`: default `PrintConfig`
* `kdl.ParseError`: thrown for all parsing errors
	* `error.msg: str`: hopefully informative
	* `error.line: int`: 1-indexed
	* `error.col: int`: 1-indexed
* `kdl.ParseFragment`: passed to converter functions
	* `pf.fragment`: slice from the source string
	* `pf.error(msg: str)` returns a `kdl.ParseError` with error location set properly already
* `kdl.nodeMatchesKey(val: Any, key: kdl.NodeKey) -> bool`
* `kdl.valueMatchesKey(val: Any, key: kdl.ValueKey) -> bool`
* `kdl.tagMatchesKey(val: str|None, key: kdl.TagKey) -> bool`
* `kdl.nameMatchesKey(val: str|None, key: kdl.NameKey) -> bool`
* `kdl.typeMatchesKey(val: Any, key: kdl.TypeKey) -> bool`
	* Functions implementing the tag/name/type matching
		used by the `node.matchesKey()` and `value.matchesKey()` methods,
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
* `kdl.KDLishValue`: a `KDLValue` or one of the supported Python native types ([see "Inserting Native Values"](#inserting-native-values))
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
	returns True.

So, for example,
`doc.getAll("foo")` would return all nodes whose name is "foo",
regardless of tag.
`doc.getAll(("my-tag", ...))` would return all nodes whose tag is "my-tag",
regardless of the node name.
`doc.getAll(re.compile(r"-"))` would return all nodes whose name starts with "-".
Etc.

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
a `"foo"` key would match any values with the tag "foo", like `(foo)1` or `(foo)"bar"`.
A `(..., kdl.Numberish)` will match any value that's a "number" type: a `Hex`, `Octal`, `Binary`, `Decimal`, `Infinity`, or `NaN`, regardless of its tag.
Etc.


## `kdlreformat`

The `kdlreformat` command-line program is installed by default
when you install this module from pypi.
It can also be run manually from the `kdlreformat.py` file
at the root of this repository
(or from the `kdl.cli.cli()` function).
Because it uses the default `version="auto"` parser and always prints KDL 2.0,
it can also be used to upgrade KDL 1.0 files to canonical KDL 2.0.

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
                        (Printing is always KDL2.) Default is 'auto'.
```
