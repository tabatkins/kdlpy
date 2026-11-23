from __future__ import annotations

import argparse
import sys

from . import errors, parsefuncs, parsing, printing, t


def cli() -> None:
    cliParser = argparse.ArgumentParser(
        prog="kdlreformat",
        description="KDL parser/printer, letting you easily reformat KDL files into a canonical representation.",
    )
    cliParser.add_argument(
        "infile",
        nargs="?",
        default="-",
    )
    cliParser.add_argument(
        "outfile",
        nargs="?",
        default="-",
    )
    cliParser.add_argument(
        "--indent",
        dest="indent",
        type=int,
        action="store",
        default=-1,
        help="How many spaces for each level of indent. -1 indicates to indent with tabs.",
    )
    cliParser.add_argument(
        "--semicolons",
        dest="semicolons",
        action="store_true",
        default=False,
        help="Whether to end nodes with semicolons or not.",
    )
    cliParser.add_argument(
        "--radix",
        dest="respectRadix",
        action="store_true",
        help="Output numeric values in the radix used by the input. (0x1a outputs as 0x1a)",
    )
    cliParser.add_argument(
        "--no-radix",
        dest="respectRadix",
        action="store_false",
        help="Convert all numeric arguments to decimal. (0x1a outputs as 26)",
    )
    cliParser.add_argument(
        "--string-type",
        dest="respectStringType",
        type=stringTypeFromString,
        action="store",
        metavar="{auto,exact,plain}",
        help=(
            "How to format strings: 'auto' (preserve raw strings, pick cleanest form otherwise; default), "
            + "'exact' (preserve input ident/quoted/raw string types), "
            + "or 'plain' (always output ident or quoted strings, never raw)."
        ),
    )
    cliParser.add_argument(
        "--raw-strings",
        dest="respectStringType",
        action="store_const",
        const=True,
        help="Alias for --string-type=exact.",
    )
    cliParser.add_argument(
        "--no-raw-strings",
        dest="respectStringType",
        action="store_const",
        const=False,
        help="Alias for --string-type=plain.",
    )
    cliParser.add_argument(
        "--exponent",
        dest="exponent",
        type=expFromString,
        action="store",
        default="e",
        help="What character to use ('e' or 'E') for indicating exponents on scinot numbers.",
    )
    cliParser.add_argument(
        "--sort-properties",
        dest="sortProperties",
        action="store_true",
        default=False,
        help="Sort node properties lexicographically by key instead of preserving insertion order.",
    )
    cliParser.add_argument(
        "--version",
        dest="version",
        type=versionFromString,
        action="store",
        default="auto",
        metavar="{1,2,auto}",
        help="KDL grammar version to parse ('1', '2', or 'auto'). Default is 'auto'.",
    )
    cliParser.set_defaults(respectRadix=True, respectStringType="auto")
    options = cliParser.parse_args()
    parseConfig = parsing.ParseConfig(
        nativeUntaggedValues=False,
        nativeTaggedValues=False,
        version=options.version,
    )
    printConfig = printing.PrintConfig(
        indent=" " * options.indent if options.indent >= 0 else "\t",
        semicolons=options.semicolons,
        respectRadix=options.respectRadix,
        respectStringType=options.respectStringType,
        exponent=options.exponent,
        sortProperties=options.sortProperties,
    )

    try:
        if options.infile == "-":
            inputText = sys.stdin.read()
        else:
            with open(options.infile, encoding="utf-8") as fh:
                inputText = fh.read()
        doc = parsefuncs.parse(inputText, parseConfig)
        outputText = doc.print(printConfig)
        if options.outfile == "-":
            sys.stdout.write(outputText)
        else:
            with open(options.outfile, "w", encoding="utf-8") as fh:
                fh.write(outputText)
    except errors.ParseError as err:
        sys.stderr.write(f"ParseError: {err}\n")
        sys.exit(1)
    except OSError as err:
        sys.stderr.write(f"Error: {err}\n")
        sys.exit(1)


def expFromString(s: str) -> str:
    if s in ("e", "E"):
        return s
    msg = f"Expected 'e' or 'E' for an exponent; got '{s}'"
    raise argparse.ArgumentTypeError(msg)


def stringTypeFromString(s: str) -> t.StringTypeMode:
    if s == "auto":
        return "auto"
    if s == "exact":
        return True
    if s == "plain":
        return False
    msg = f"Expected 'auto', 'exact', or 'plain' for string type; got '{s}'"
    raise argparse.ArgumentTypeError(msg)


def versionFromString(s: str) -> t.ParseVersion:
    if s == "1":
        return 1
    if s == "2":
        return 2
    if s == "auto":
        return "auto"
    msg = f"Expected '1', '2', or 'auto' for KDL version; got '{s}'"
    raise argparse.ArgumentTypeError(msg)
