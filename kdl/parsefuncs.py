from __future__ import annotations

from . import converters, parsing, t, types
from .errors import ParseError, ParseFragment
from .result import Result
from .stream import Stream


def parse(input: str, config: t.ParseConfig | None = None) -> t.Document:
    if config is None:
        config = parsing.defaults
    doc = types.Document()
    s = Stream(input, config)
    i = parseLinespace(s, 0).i
    while not s.eof(i):
        node, i, err = parseNode(s, i).vie
        if err:
            raise ParseError(s, i, "Expected a node")
        if node:
            doc.nodes.append(node)
        i = parseLinespace(s, i).i
    return doc


def parseNode(s: Stream, start: int) -> Result[types.Node | None]:
    i = start

    sd, i = parseSlashDash(s, i).vi
    if sd is None:
        sd = False

    # tag?
    tag, i = parseTag(s, i).vi
    if tag is None:
        tag = None

    # name
    name, i = parseIdent(s, i).vi
    if name is None:
        return Result.fail(start)

    nameEnd = i

    node = types.Node(tag=tag, name=name)

    # props and args
    entryNames: set[str] = set()
    while True:
        space, i = parseNodespace(s, i).vi
        if space is None:
            break
        entry, i, err = parseEntry(s, i).vie
        if err:
            break
        if entry is None:
            continue
        if entry[0] is not None and entry[0] in entryNames:
            # repeated property name, replace the existing value
            for i, (name, _) in enumerate(node.entries):
                if name == entry[0]:
                    node.entries[i] = entry
                    break
        else:
            node.entries.append(entry)
            if entry[0] is not None:
                entryNames.add(entry[0])

    i = parseNodespace(s, i).i

    nodes, i = parseNodeChildren(s, i).vi
    if nodes is not None:
        node.nodes = nodes

    i = parseNodespace(s, i).i
    i = parseNodeTerminator(s, i).i

    if sd:
        return Result(None, i)
    else:
        for key, converter in s.config.nodeConverters.items():
            if node.matchesKey(key):
                node = converter(
                    node,
                    ParseFragment(s[start:nameEnd], s, start),
                )
                if node == NotImplemented:
                    continue
                else:
                    break
        return Result(node, i)


def parseNodeChildren(s: Stream, start: int) -> Result[list[types.Node] | None]:
    sd, i = parseSlashDash(s, start).vi
    if sd is None:
        sd = False

    if s[i] != "{":
        return Result.fail(start)
    i += 1
    nodes = []
    while True:
        i = parseLinespace(s, i).i
        node, i = parseNode(s, i).vi
        if node is None:
            break
        nodes.append(node)
    i = parseLinespace(s, i).i
    if s.eof(i):
        raise ParseError(s, start, "Hit EOF while searching for end of child list")
    if s[i] != "}":
        raise ParseError(s, i, "Junk between end of child list and closing }")
    if sd:
        return Result(None, i + 1)
    else:
        return Result(nodes, i + 1)


def parseTag(s: Stream, start: int) -> Result[str]:
    if s[start] != "(":
        return Result.fail(start)
    tag, end = parseIdent(s, start + 1).vi
    if tag is None:
        return Result.fail(start)
    if s[end] != ")":
        raise ParseError(s, end, "Junk between tag ident and closing paren.")
    return Result(tag, end + 1)


def parseIdent(s: Stream, start: int) -> Result[str]:
    string, i = parseString(s, start).vi
    if string is not None:
        return Result(string.value, i)
    return parseBareIdent(s, start)


def parseBareIdent(s: Stream, start: int) -> Result[str]:
    res, i = parseIdentStart(s, start).vi
    if res is None:
        return Result.fail(start)
    while isIdentChar(s[i]):
        i += 1
    ident = s[start:i]
    return Result(ident, i)


def parseIdentStart(s: Stream, start: int) -> Result[str]:
    if isDigit(s[start]) or (isSign(s[start]) and isDigit(s[start + 1])):
        return Result.fail(start)
    if not isIdentChar(s[start]):
        return Result.fail(start)
    return Result(s[start], start + 1)


def parseNodeTerminator(s: Stream, start: int) -> Result[str | bool]:
    res = parseNewline(s, start)
    if res.valid:
        return res
    res = parseSingleLineComment(s, start)
    if res.valid:
        return res
    if s[start] == ";":
        return Result(";", start + 1)
    if s.eof(start):
        return Result(True, start)
    raise ParseError(s, start, "Junk after node, before terminator.")


def parseEntry(s: Stream, start: int) -> Result[tuple[str | None, types.Value] | None]:
    sd, i = parseSlashDash(s, start).vi
    if sd is None:
        sd = False

    ent, i = parseProperty(s, i).vi
    if ent is None:
        ent, i = parseValue(s, i).vi
        if ent is None:
            return Result.fail(start)
    if sd:
        return Result(None, i)
    else:
        return Result(ent, i)


def parseProperty(s: Stream, start: int) -> Result[tuple[str | None, types.Value] | None]:
    key, i = parseIdent(s, start).vi
    if key is None:
        return Result.fail(start)
    if s[i] != "=":
        # property name might be a string,
        # so this isn't point-of-no-return yet
        return Result.fail(start)
    entity, i = parseValue(s, i + 1).vi
    if entity is None:
        raise ParseError(s, i, "Expected value after prop=.")
    return Result((key, entity[1]), i)


def parseValue(s: Stream, start: int) -> Result[tuple[str | None, t.Any] | None]:
    tag, i = parseTag(s, start).vi

    valueStart = i
    val: t.Any
    val, i = parseNumber(s, i).vi
    if val is None:
        val, i = parseKeyword(s, i).vi
        if val is None:
            val, i = parseString(s, i).vi
    if val is not None:
        val.tag = tag
        for key, converter in s.config.valueConverters.items():
            if val.matchesKey(key):
                val = converter(
                    val,
                    ParseFragment(s[valueStart:i], s, i),
                )
                if val == NotImplemented:
                    continue
                else:
                    break
        else:
            if tag is None and s.config.nativeUntaggedValues:
                val = val.value
            if tag is not None and s.config.nativeTaggedValues:
                val = converters.toNative(val, ParseFragment(s[valueStart:i], s, i))
        return Result((None, val), i)

    if s[i] == "'":
        raise ParseError(s, i, "KDL strings use double-quotes.")

    ident = parseBareIdent(s, i).value
    if ident is not None and ident.lower() in ("true", "false", "null"):
        raise ParseError(s, i, "KDL keywords are lower-case.")

    # Failed to find a value
    # But if I found a tag, something's up
    if tag is not None:
        raise ParseError(s, i, "Found a tag, but no value following it.")
    return Result.fail(start)


def parseNumber(s: Stream, start: int) -> Result[types.Numberish]:
    if not isNumberStart(s, start):
        return Result.fail(start)
    res: Result[types.Numberish] = parseBinaryNumber(s, start)
    if res.valid:
        return res
    res = parseOctalNumber(s, start)
    if res.valid:
        return res
    res = parseHexNumber(s, start)
    if res.valid:
        return res
    res = parseDecimalNumber(s, start)
    if res.valid:
        return res
    raise ParseError(
        s,
        start,
        "Expected a number, but got junk after the initial digit.",
    )


def parseBinaryNumber(s: Stream, start: int) -> Result[types.Binary]:
    i = start

    # optional sign
    sign, i = parseSign(s, i).vi
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "b"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isBinaryDigit(s[i]):
        raise ParseError(s, i, "Expected binary digit after 0b, got junk.")

    # following digits/underscores
    end = i + 1
    while isBinaryDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 2) * sign
    return Result(types.Binary(value), end)


def parseOctalNumber(s: Stream, start: int) -> Result[types.Octal]:
    i = start

    # optional sign
    sign, i = parseSign(s, i).vi
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "o"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isOctalDigit(s[i]):
        raise ParseError(s, i, "Expected octal digit after 0o, got junk.")

    # following digits/underscores
    end = i + 1
    while isOctalDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 8) * sign
    return Result(types.Octal(value), end)


def parseHexNumber(s: Stream, start: int) -> Result[types.Hex]:
    i = start

    # optional sign
    sign, i = parseSign(s, i).vi
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "x"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isHexDigit(s[i]):
        raise ParseError(s, i, "Expected hex digit after 0x, got junk.")

    # following digits/underscores
    end = i + 1
    while isHexDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 16) * sign
    return Result(types.Hex(value), end)


def parseDecimalNumber(s: Stream, start: int) -> Result[types.Decimal]:
    i = start

    # optional sign
    i = parseSign(s, i).i

    # integer part
    i = parseDigits(s, i).i

    if s[i] == ".":
        result, i = parseDigits(s, i + 1).vi
        if result is None:
            raise ParseError(s, i, "Expected digit after decimal point.")

    mantissaChars = s[start:i].replace("_", "")
    mantissa: int | float
    try:
        mantissa = int(mantissaChars, 10)
    except ValueError:
        try:
            mantissa = float(mantissaChars)
        except ValueError as exc:
            raise ParseError(
                s,
                start,
                "Number-like string didn't actually parse as a number.",
            ) from exc

    exponent = 0
    if s[i] in ("e", "E"):
        exponentStart = i + 1
        i = parseSign(s, i + 1).i
        result, i = parseDigits(s, i).vi
        if result is None:
            raise ParseError(s, i, "Expected number after exponent.")
        exponent = int(s[exponentStart:i].replace("_", ""))

    return Result(types.Decimal(mantissa, exponent), i)


def parseDigits(s: Stream, start: int) -> Result[bool]:
    # First digit must be decimal digit
    # Subsequent can be digit or underscore
    if not isDigit(s[start]):
        return Result.fail(start)
    end = start + 1
    while isDigit(s[end]) or s[end] == "_":
        end += 1
    return Result(True, end)


def isNumberStart(s: Stream, start: int) -> bool:
    # all numbers start with an optional sign
    # followed by a digit:
    # either the first digit of the number,
    # or the 0 of the prefix)
    if isDigit(s[start]):
        return True
    if isSign(s[start]) and isDigit(s[start + 1]):
        return True
    return False


def parseSign(s: Stream, start: int) -> Result[int]:
    if s[start] == "+":
        return Result(1, start + 1)
    if s[start] == "-":
        return Result(-1, start + 1)
    return Result.fail(start)


def parseKeyword(s: Stream, start: int) -> Result[types.Bool | types.Null | types.Infinity | types.NaN]:
    if s[start] != "#":
        return Result.fail(start)
    i = start + 1
    ident, i = parseBareIdent(s, i).vi
    if ident is None:
        return Result.fail(start)
    if ident == "true":
        return Result(types.Bool(True), i)
    elif ident == "false":
        return Result(types.Bool(False), i)
    elif ident == "null":
        return Result(types.Null(), i)
    elif ident == "inf":
        return Result(types.Infinity(float("inf")), i)
    elif ident == "-inf":
        return Result(types.Infinity(float("-inf")), i)
    elif ident == "nan":
        return Result(types.NaN(float("nan")), i)
    elif ident.lower() in ("true", "false", "null", "inf", "-inf", "nan"):
        raise ParseError(s, start, f"KDL keywords must be written in lowercase, got #{ident}")
    else:
        raise ParseError(s, start, f"Unknown keyword #{ident}")


def parseString(s: Stream, start: int) -> Result[types.Stringish]:
    if s[start] == '"':
        return parseEscapedString(s, start)
    if s[start] == "r" and s[start + 1] in ("#", '"'):
        return parseRawString(s, start)
    return Result.fail(start)


def parseEscapedString(s: Stream, start: int) -> Result[types.String]:
    if s[start] != '"':
        return Result.fail(start)
    i = start + 1

    rawChars = ""
    while True:
        if s[i] not in ("\\", '"', ""):
            rawChars += s[i]
            i += 1
            continue
        if s[i] == '"':
            break
        if s[i] == "":
            raise ParseError(
                s,
                start,
                "Hit EOF while looking for the end of the string",
            )
        ch, i = parseEscape(s, i).vi
        if ch is None:
            raise ParseError(s, i, "Invalid escape sequence in string")
        rawChars += ch

    return Result(types.String(rawChars), i + 1)


def parseEscape(s: Stream, start: int) -> Result[str]:
    if s[start] != "\\":
        return Result.fail(start)
    ch = s[start + 1]
    if ch == "n":
        return Result("\n", start + 2)
    if ch == "r":
        return Result("\r", start + 2)
    if ch == "t":
        return Result("\t", start + 2)
    if ch == "\\":
        return Result("\\", start + 2)
    if ch == "/":
        return Result("/", start + 2)
    if ch == '"':
        return Result('"', start + 2)
    if ch == "b":
        return Result("\b", start + 2)
    if ch == "f":
        return Result("\f", start + 2)
    if ch == "u":
        if s[start + 2] != "{":
            raise ParseError(
                s,
                start,
                "Unicode escapes must surround their codepoint in {}",
            )
        i = start + 3
        hexStart = i
        while isHexDigit(s[i]):
            i += 1
        hexCount = i - hexStart
        if s[i] != "}":
            raise ParseError(s, hexStart, "Expected } to finish a unicode escape")
        if hexCount < 1:
            raise ParseError(s, hexStart, "Unicode escape doesn't contain a codepoint")
        if hexCount > 6:
            raise ParseError(
                s,
                hexStart,
                "Unicode escapes can contain at most six digits",
            )
        hexValue = int(s[hexStart:i], 16)
        if hexValue > 0x10FFFF:
            raise ParseError(
                s,
                hexStart,
                "Maximum codepoint in a unicode escape is 0x10ffff",
            )
        return Result(chr(hexValue), i + 1)
    raise ParseError(s, start, "Invalid character escape")


def parseRawString(s: Stream, start: int) -> Result[types.RawString]:
    if s[start] != "r":
        return Result.fail(start)
    i = start + 1

    # count hashes
    hashCount, i = parseInitialHashes(s, i).vi
    assert hashCount is not None

    if s[i] != '"':
        return Result.fail(start)
    i = i + 1

    stringStart = i
    while True:
        while s[i] not in ('"', ""):
            i += 1
        if s[i] == "":
            raise ParseError(
                s,
                start,
                "Hit EOF while looking for the end of the raw string.",
            )
        stringEnd = i
        i += 1
        result, i = parseFinalHashes(s, i, expectedCount=hashCount).vi
        if result is not None:
            return Result(types.RawString(s[stringStart:stringEnd]), i)


def parseInitialHashes(s: Stream, start: int) -> Result[int]:
    i = start
    while s[i] == "#":
        i += 1
    if s[i] == '"':
        return Result(i - start, i)
    return Result.fail(start)


def parseFinalHashes(s: Stream, start: int, expectedCount: int) -> Result[bool]:
    i = start
    while s[i] == "#":
        i += 1
    count = i - start
    if count < expectedCount:
        return Result.fail(start)
    if count > expectedCount:
        raise ParseError(
            s,
            start,
            f"Expected {expectedCount} hashes at end of raw string; got {count}.",
        )
    return Result(True, i)


def parseNewline(s: Stream, start: int) -> Result[bool]:
    if s[start] == "\x0d" and s[start + 1] == "\x0a":
        return Result(True, start + 2)
    if isNewlineChar(s[start]):
        return Result(True, start + 1)
    return Result.fail(start)


def parseLinespace(s: Stream, start: int) -> Result[bool]:
    i = start
    while True:
        nl, i = parseNewline(s, i).vi
        ws, i = parseWhitespace(s, i).vi
        sc, i = parseSingleLineComment(s, i).vi
        if nl is None and ws is None and sc is None:
            break
    if i == start:
        return Result.fail(start)
    return Result(True, i)


def parseNodespace(s: Stream, start: int) -> Result[bool]:
    i = start
    while True:
        i = parseWhitespace(s, i).i
        escline, i = parseEscline(s, i).vi
        if escline is None:
            break
    if i == start:
        return Result.fail(start)
    return Result(True, i)


def parseEscline(s: Stream, start: int) -> Result[bool]:
    if s[start] != "\\":
        return Result.fail(start)
    i = parseWhitespace(s, start + 1).i
    if s[i] == "\n":
        return Result(True, i + 1)
    sl, i = parseSingleLineComment(s, i).vi
    if sl is not None:
        return Result(True, i)
    return Result.fail(start)


def parseWhitespace(s: Stream, start: int) -> Result[bool]:
    i = start
    while True:
        sp, i = parseUnicodeSpace(s, i).vi
        bc, i = parseBlockComment(s, i).vi
        if sp is None and bc is None:
            break
    if i == start:
        return Result.fail(start)
    return Result(True, i)


def parseUnicodeSpace(s: Stream, start: int) -> Result[bool]:
    if not isWSChar(s[start]):
        return Result.fail(start)
    end = start + 1
    while isWSChar(s[end]):
        end += 1
    return Result(True, end)


def isIdentChar(ch: str) -> bool:
    if not ch:
        return False
    if ch in r'''\/(){}<>;[]=,"''':
        return False
    if isLinespaceChar(ch):
        return False
    cp = ord(ch)
    if cp <= 0x20:
        return False
    if cp > 0x10FFFF:
        return False
    return True


def isKeyword(ident: str) -> bool:
    return ident in ("null", "true", "false")


def isSign(ch: str) -> bool:
    return ch != "" and ch in "+-"


def isDigit(ch: str) -> bool:
    return ch != "" and ch in "0123456789"


def isBinaryDigit(ch: str) -> bool:
    return ch != "" and ch in "01"


def isOctalDigit(ch: str) -> bool:
    return ch != "" and ch in "01234567"


def isHexDigit(ch: str) -> bool:
    return ch != "" and ch in "0123456789abcdefABCDEF"


def isWSChar(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    if cp in (0x09, 0x20, 0xA0, 0x1680, 0x202F, 0x205F, 0x3000, 0xFEFF):
        return True
    if 0x2000 <= cp <= 0x200A:
        return True
    return False


def isNewlineChar(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return cp in (0x0A, 0x0D, 0x85, 0x0C, 0x2028, 0x2029)


def isLinespaceChar(ch: str) -> bool:
    if not ch:
        return False
    return isWSChar(ch) or isNewlineChar(ch)


def parseSlashDash(s: Stream, start: int) -> Result[bool]:
    if s[start] == "/" and s[start + 1] == "-":
        i = parseNodespace(s, start + 2).i
        return Result(True, i)
    return Result.fail(start)


def parseSingleLineComment(s: Stream, start: int) -> Result[bool]:
    if not (s[start] == "/" and s[start + 1] == "/"):
        return Result.fail(start)
    i = start + 2
    while not isNewlineChar(s[i]) and not s.eof(i):
        i += 1
    i = parseNewline(s, i).i
    return Result(True, i)


def parseBlockComment(s: Stream, start: int) -> Result[bool]:
    if not (s[start] == "/" and s[start + 1] == "*"):
        return Result.fail(start)
    i = start + 2
    while True:
        if s.eof(i):
            raise ParseError(s, start, "Hit EOF while inside a multiline comment")
        if s[i] == "*" and s[i + 1] == "/":
            return Result(True, i + 2)
        if s[i] == "/" and s[i + 1] == "*":
            i = parseBlockComment(s, i).i
            continue
        i += 1
