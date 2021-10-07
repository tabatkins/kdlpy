# KDL.py

A handwritten Python 3.7+ implemenation of a parser
for the [KDL Document Language](https://kdl.dev),
fully compliant with KDL 1.0.0.

[KDL](https://kdl.dev) is, as the name suggests, a document language,
filling approximately the same niche as JSON/YAML/XML/etc.
It combines the best of several of these languages,
while avoiding their pitfalls:
more general than JSON and more powerful than XML,
while avoiding the verbosity of XML
or the explosive complexity of YAML.

## Using

The `kdl.parse(str, parseConfig?)` function parses, you guessed it, a string of KDL into a KDL document object:

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
    children=[
        Node(
            name='node',
            values=['arg'],
            children=[
                Node(
                    name='child',
                    properties=OrderedDict([
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

```
>>> doc.children[0].children[0].properties["foo"] = 2
>>>
>>> print(doc)
node_name "arg" {
        child_node bar=True foo=2
}

```

Stringifying a `kdl.Document` object will produce a valid KDL document back. You can also call `doc.print(printConfig?)` to customize the printing with a `PrintConfig` object, described below.  See below for how to configure printing options.

## Customizing Parsing

Parsing can be controlled via a `kdl.ParseConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `ParseConfig` object to `kdl.parse(str, ParseConfig?)`
	or `parser.parse(str, ParseConfig?)`
	(if you've constructed a `kdl.Parser`).
2. Creating a `kdl.Parser(parseConfig?, printConfig?)`,
	which automatically applies it to its `.parse()` method if not overriden.
3. Fiddling with the `kdl.parsing.defaults` object,
	which is used if nothing else provides a config.

A `ParserConfig` object has the following properties:

* `nativeUntaggedValues: bool = True`

	Controls whether the parser produces native Python objects (`str`, `int`, `None`, etc) when parsing untagged values (those without a `(foo)` prefix), or always produces kdlpy objects (such as `String`, `Decimal`, etc).

	Tagged values like `(date)"2021-01-01"` always become kdlpy objects (in this case, `kdl.String("2021-01-01", tag="date"))`.

## Customizing Printing

Like parsing, printing a kdlpy `Document` back to a KDL string can be controlled by a `kdl.PrintConfig` object,
which can be provided in three ways.
In order of importance:

1. Passing a `PrintConfig` object to `doc.print(PrintConfig?)`.
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

	Again, this only has an effect on kdlpy objects;
	native Python numbers are printed as normal for Python.

* `exponent: str = "e"`

	What character to use for the exponent part of decimal numbers,
	when printed with scientific notation.
	Should only be set to "e" or "E".

	Like the previous options, this only has an effect on kdlpy objects;
	native Python numbers are printed as normal for Python.

## Full API Reference

~in progress~

* `kdl.parse(str, config: kdl.ParseConfig?) -> kdl.Document`
* `kdl.Parser(parseConfig: kdl.ParseConfig?, printConfig: kdl.PrintConfig?)`
	* `parser.parse(str, config: kdl.ParseConfig?) -> kdl.Document`
	* `parser.print(config: kdl.PrintConfig?) -> str`
* `kdl.Document(children: list[kdl.Node]?, printConfig: kdl.PrintConfig?)`
	* `doc.print(PrintConfig?) -> str`
* `kdl.Node(name: str, tag: str?, values: list[Any]?, props: dict[str, Any]?, children: list[kdl.Node]?)`
* `kdl.Binary(value: int, tag: str?)`
* `kdl.Octal(value: int, tag: str?)`
* `kdl.Decimal(mantissa: int|float, exponent: int?, tag: str?)`
	* `dec.value`: readonly, `mantissa * (10**exponent)`
* `kdl.Hex(value: int, tag: str?)`
* `kdl.Bool(value: bool, tag: str?)`
* `kdl.Null(tag: str?)`
	* `null.value`: readonly, always `None`
* `kdl.RawString(value: str, tag: str?)`
* `kdl.String(value: str, tag: str?)`
* `kdl.ParseConfig(...)` see above for options
	* `kdl.parsing.defaults`: default `ParseConfig`
* `kdl.PrintConfig(...)` see above for options
	* `kdl.printing.defaults`: default `PrintConfig`
* `kdl.ParseError`: thrown for all parsing errors
	* `error.msg: str`: hopefully informative
	* `error.line: int`: 1-indexed
	* `error.col: int`: 1-indexed