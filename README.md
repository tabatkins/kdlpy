# KDL-py

A handwritten Python 3.7+ implemenation of a parser
for the [KDL Document Language](https://kdl.dev),
fully compliant with KDL 1.0.0.

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
import kdl

>>> import kdl
>>> doc = kdl.parse('''
... node_name "arg" {
...     child_node foo=1 bar=true
... }
... ''')
>>>
>>> doc
Document(
    nodes=[
        Node(
            name='node',
            args=['arg'],
            nodes=[
                Node(
                    name='child',
                    props=OrderedDict([
                        ('foo', 1.0),
                        ('bar', True)
                    ])
                )
            ]
        )
    ]
)
```

You can also create a `kdl.Parser()` object and call its `.parse()` method; `Parser` objects can set up parsing and printing options that'll apply by default. See below for how to configure parsing options.

Either way, you'll get back a `kdl.Document` object, which is fully mutable. By default, untagged KDL values are represented with native Python objects.

```py3
>>> doc.nodes[0].nodes[0].props["foo"] = 2
>>>
>>> print(doc)
node_name "arg" {
        child_node foo=2 bar=true
}

```

Stringifying a `kdl.Document` object will produce a valid KDL document back. You can also call `doc.print(printConfig|None)` to customize the printing with a `PrintConfig` object, described below.  See below for how to configure printing options.

### Inserting Native Values

kdl-py allows a number of native Python objects to be used directly in KDL documents by default,
and allows you to customize your own objects for use.

kdl-py automatically recognizes and correctly serializes the following objects:

* `bool`: as untagged `true` or `false`
* `None`: as untagged `null`
* `int`, `float`: as untagged decimal number
* `str`: as untagged string
* `bytes`: as `(base64)`-tagged string
* `decimal.Decimal`: as `(decimal)`-tagged string
* `datetime`, `date`, and `time`: as `(date-time)`, `(date)`, or `(time)`-tagged strings
* `ipaddress.IPv4Address` and `ipaddress.IPv6Address`: as `(ipv4)` or `(ipv6)`-tagged strings
* `urllib.parse.ParseResult` (the result of calling `urllib.parse.urlparse()`): as `(url)`-tagged string
* `uuid.UUID`: as `(uuid)`-tagged string
* `re.Pattern` (the result of calling `re.compile()`): as `(regex)`-tagged raw string

All of the tags used above are reserved and predefined by the [KDL specification](https://github.com/kdl-org/kdl/blob/main/SPEC.md#reserved-type-annotations-for-numbers-without-decimals).

In addition, any value with a `.to_kdl()` method
can be used in a kdl-py document.
The method will be called when the document is stringified,
and must return one of the kdl-py types,
or any of the native types defined above.

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
	which automatically applies it to its `.parse()` method if not overriden.
3. Fiddling with the `kdl.parsing.defaults` object,
	which is used if nothing else provides a config.

A `ParseConfig` object has the following properties:

* `nativeUntaggedValues: bool = True`

	Controls whether the parser produces native Python objects (`str`, `int`, `float`, `bool`, `None`) when parsing untagged values (those without a `(foo)` prefix), or always produces kdl-py objects (such as `kdl.String`, `kdl.Decimal`, etc).

* `nativeTaggedValues: bool = True`

	Controls whether the parser produces native Python objects
	when parsing *tagged* values,
	for some of [KDL's predefined tags](https://github.com/kdl-org/kdl/blob/main/SPEC.md#reserved-type-annotations-for-numbers-without-decimals):

	* `i8`, `i16`, `i32`, `i64`, `u8`, `u16`, `u32`, `u64` on numbers:
		Checks that the value is in the specified range,
		then converts it to an `int`.
		(It will serialize back out as an ordinary untagged number.)
	* `f32`, `f64` on numbers:
		Converts it to a `float`.
		(It will serialize back out as an ordinary untagged number.)
	* `decimal64`, `decimal128` on numbers, and `decimal` on strings:
		Converts it to a `decimal.Decimal` object.
		(Always reserializes to a `(decimal)`-tagged string.)
	* `date-time`, `date`, `time` on strings:
		Converts to a `datetime`, `time`, or `date` object.
	* `ipv4`, `ipv6` on strings:
		Converts it to an `ipaddress.IPv4Address` or `ipaddress.IPv6Address` object.
	* `url` on strings:
		Converts it to a `urllib.parse.ParseResult` tuple.
	* `uuid` on strings:
		Converts it to a `uuid.UUID` object.
	* `regex` on strings:
		Converts it to a `re.Pattern` object.
		(It will serialize back out as a raw string.)
	* `base64` on strings:
		Converts it to a `bytes` object.


* `valueConverters: Dict[str, Callable] = {}`

	A dictionary of tag->converter functions,
	letting you parse tagged values
	(like `(date)"2021-01-01"`)
	into whatever types you'd like.

	Whenever a value is encountered with the given tag,
	your converter will be called with two arguments:
	the fully-constructed kdl-py object,
	and a `ParseFragment` object giving you access
	to the precise characters parsed from the document.
	Whatever you return will be inserted into the document instead.

	(Note that this does *not* specialize on value type;
	a converter set to handle, say, a "base6" tag,
	intending it to be used on numbers like `(base6)123450`,
	will get called for `(base6)"a string"` too.
	If you intend to only handle specific types of values,
	make sure to check the value's type
	and return it unchanged if you don't intend to handle it.)

	You can produce KDL values
	(such as parsing `(hex)"0x12.e5"` into a `kdl.Decimal`,
	since KDL doesn't support fractional hex values),
	or produce any other Python type.
	If you return a non-KDL type,
	you probably want to ensure it has a `.to_kdl()` method
	(or is one of the supported built-in types),
	so it can be serialized back into a KDL document.

* `nodeConverters: Dict[NodeKey, Callable] = {}`

	Similar to `valueConverters`,
	except the converters here are called on `kdl.Node`s.

	The keys for the map are [`NodeKey`s](#NodeKey), as well,
	because both the node name *and* tag are valuable to key off of.


### ParseFragment

`kdl.ParseFragment` is passed to your custom converters,
specified in `kdl.ParseConfig.tags`,
giving you direct access to the input characters
before any additional processing was done on them.
This is useful, for example,
to handle numeric types
that might have lost precision in the normal parse.

It exposes a `.fragment` property,
containing the raw text of the value
(after the tag, if any).

It also exposes a `.error(str)` method,
which takes a custom error message
and returns a `kdl.ParseError`
with the `ParseFragment`'s location already built in,
ready for you to `raise`.
This should be used if your conversion fails for any reason,
so your errors look the same as native parse errors.

## Customizing Printing

Like parsing, printing a kdl-py `Document` back to a KDL string can be controlled by a `kdl.PrintConfig` object,
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

	When `False`, automatically skips over any "null"/`None` arguments.
	This will corrupt documents that use the "null" keyword intentionally,
	but can be useful if you'd prefer to use a `None` value
	as a signal that the argument has been removed.

* `printNullProps: bool = True`

	Identical to `printNullArgs`,
	but applies to properties rather than arguments.

* `respectStringType: bool = True`

	When `True`, the printer will output strings as the same type they were in the input,
	either raw (`r#"foo"#`) or normal (`"foo"`).
	When `False`, the printer always outputs normal strings.

	Note that this only has an effect on `kdl.String` and `kdl.RawString` objects;
	if the document contains Python `str` objects,
	they will always output as normal strings.

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

## Full API Reference

* `kdl.parse(str, config: kdl.ParseConfig|None) -> kdl.Document`
* `kdl.Parser(parseConfig: kdl.ParseConfig|None, printConfig: kdl.PrintConfig|None)`
	* `parser.parse(str, config: kdl.ParseConfig|None) -> kdl.Document`
	* `parser.print(config: kdl.PrintConfig|None) -> str`
* `kdl.Document(nodes: list[kdl.Node]?, printConfig: kdl.PrintConfig|None)`
	* `doc.print(PrintConfig|None) -> str`
	* `doc[NodeKey] -> Node` returns the first child node matching the [`NodeKey`](#NodeKey). Raises a `KeyError` if nothing matches the `NodeKey`, similar to a `dict`.
	* `doc.get(NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#NodeKey). Returns the default value if nothing matches.
	* `doc.getAll(NodeKey) -> Iterable[kdl.Node]` returns all child nodes matching the [`NodeKey`](#NodeKey)
* `kdl.Node(name: str, tag: str|None, args: list[Any]?, props: dict[str, Any]?, nodes: list[kdl.Node]?)`
	* `node[NodeKey] -> Node` returns the first child node matching the [`NodeKey`](#NodeKey). Raises a `KeyError` if nothing matches the `NodeKey`, similar to a `dict`.
	* `node.get(NodeKey, default: T = None) -> kdl.Node | T` returns the first child node matching the [`NodeKey`](#NodeKey). Returns the default value if nothing matches.
	* `node.getAll(NodeKey) -> Iterable[kdl.Node]` returns all child nodes matching the [`NodeKey`](#NodeKey)
	
* `kdl.Binary(value: int, tag: str|None)`
* `kdl.Octal(value: int, tag: str|None)`
* `kdl.Decimal(mantissa: int|float, exponent: int|None, tag: str|None)`
	* `dec.value`: readonly, `mantissa * (10**exponent)`
* `kdl.Hex(value: int, tag: str|None)`
* `kdl.Bool(value: bool, tag: str|None)`
* `kdl.Null(tag: str|None)`
	* `null.value`: readonly, always `None`
* `kdl.RawString(value: str, tag: str|None)`
* `kdl.String(value: str, tag: str|None)`
* `kdl.ExactValue(chars: str, tag: str|None)` †
* `kdl.Value`, `kdl.Numberish`, `kdl.Stringish` ‡
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

† Not produced by the parser.
Can be returned by a user's `.to_kdl()` method
if they want to produce a value *precisely* in a particular syntax,
in a way that the built-in kdl-py classes don't.

‡ Not produced by the parser.
These are abstract base classes to help in type testing:
`Value` matches all eight value classes,
`Numberish` matches all four numeric value classes,
and `Stringish` matches both string value classes.

A few type aliases also exist,
used by the module in a few places
and potentially useful for your code:

* `kdl.KDLAny`: a `Document`, `Node`, or any of the `Value` subtypes
* `kdl.KDLValue`: any of the `Value` subtypes
* `kdl.KDLishValue`: a `KDLValue` or one of the supported Python native types [see "Inserting Native Types"](#inserting-native-types)

These aliases only exist when `typing.TYPE_CHECKING` is true,
so they're *only* useful for writing types;
they won't be visible at runtime.

### `NodeKey`

A few data structures and functions take a `NodeKey`
to match against a node.

Formally, `NodeKey` is `str | tuple[str|None, str|None]`;
that is,
either a string,
or a tuple of optional strings.

If it's a `str`,
then it matches any node whose **name** is that value,
regardless of the tag.

If it's a tuple,
then it matches any node whose tag is the first value
(including `None` to match nodes without a tag),
and whose name is the second value
(where `None` matches any node, regardless of name).

That is, the possible variants are:

* `"nodename"`: matches nodes with that name, regardless of tag.
* `("tagname", None)`: matches nodes with that tag, regardless of name.
* `("tagname", "nodename")`: matches nodes with that tag and name.
* `(None, None)`: matches nodes with no tag, regardless of name.
* `(None, "nodename")`: matches nodes with **no tag**, and that name.




## `kdlreformat`

The `kdlreformat` command-line program is installed by default
when you install this module from pypi.
It can also be run manually from the `kdlreformat.py` file
at the root of this repository
(or from the `kdl.cli.cli()` function)

```
usage: kdlreformat [-h] [--indent INDENT] [--semicolons] [--radix]
                   [--no-radix] [--raw-strings] [--no-raw-strings]
                   [--exponent EXPONENT]
                   [infile] [outfile]

KDL parser/printer, letting you easily reformat KDL files into a canonical
representation.

positional arguments:
  infile
  outfile

optional arguments:
  -h, --help           show this help message and exit
  --indent INDENT      How many spaces for each level of indent. -1 indicates
                       to indent with tabs.
  --semicolons         Whether to end nodes with semicolons or not.
  --radix              Output numeric values in the radix used by the input.
                       (0x1a outputs as 0x1a)
  --no-radix           Convert all numeric arguments to decimal. (0x1a outputs
                       as 26)
  --raw-strings        Output string values in the string type used by the
                       input.
  --no-raw-strings     Convert all string arguments into plain strings.
  --exponent EXPONENT  What character to use ('e' or 'E') for indicating
                       exponents on scinot numbers.
```
