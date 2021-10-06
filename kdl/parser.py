from __future__ import annotations
import collections
import typing
from typing import Union

from . import types
from .errors import ParseError
from .stream import Stream
from .result import Result


def parse(input):
    return parseDocument(Stream(input), 0)


def parseDocument(s: Stream, start: int = 0) -> types.Document:
    doc = types.Document()
    _, i = parseLinespace(s, start)
    while True:
        if s.eof(i):
            return doc
        node, i = parseNode(s, i)
        if node is None:
            raise ParseError(s, i, "Expected a node")
        doc.children.append(node)
        _, i = parseLinespace(s, i)


def parseNode(s: Stream, start: int) -> Result:
    i = start

    sd, i = parseSlashDash(s, i)

    # tag?
    tag, i = parseTag(s, i)

    # name
    name, i = parseIdent(s, i)
    if name is None:
        return Result.fail(start)

    # props and values
    entities = []
    while True:
        space, i = parseNodespace(s, i)
        if space is None:
            break
        entity, i = parseEntity(s, i)
        if entity is not None:
            entities.append(entity)
            continue
        break

    _, i = parseNodespace(s, i)

    children, i = parseNodeChildren(s, i)
    if children is None:
        children = types.Children()

    _, i = parseNodespace(s, i)
    _, i = parseNodeTerminator(s, i)

    return Result(
        types.Node(
            name=name, tag=tag, entities=entities, children=children, escaped=bool(sd)
        ),
        i,
    )


def parseNodeChildren(s: Stream, start: int) -> Result:
    sd, i = parseSlashDash(s, start)

    if s[i] != "{":
        return Result.fail(start)
    i += 1
    nodes = []
    while True:
        _, i = parseLinespace(s, i)
        node, i = parseNode(s, i)
        if node is not None:
            nodes.append(node)
        else:
            break
    _, i = parseLinespace(s, i)
    if s.eof(i):
        raise ParseError(s, start, "Hit EOF while searching for end of child list")
    if s[i] != "}":
        raise ParseError(s, i, "Junk between end of child list and closing }")
    return Result(types.Children(nodes, escaped=bool(sd)), i + 1)


def parseTag(s: Stream, start: int) -> Result:
    if s[start] != "(":
        return Result.fail(start)
    tag, end = parseIdent(s, start + 1)
    if tag is None:
        return Result.fail(start)
    if s[end] != ")":
        raise ParseError(s, end, f"Junk between tag ident and closing paren.")
    return Result(tag, end + 1)


def parseIdent(s: Stream, start: int) -> Result:
    string, i = parseString(s, start)
    if string is not None:
        return Result(string.value, i)
    return parseBareIdent(s, start)


def parseBareIdent(s: Stream, start: int) -> Result:
    res, i = parseIdentStart(s, start)
    if not res:
        return Result.fail(start)
    while isIdentChar(s[i]):
        i += 1
    ident = s[start:i]
    if isKeyword(ident):
        return Result.fail(start)
    return Result(ident, i)


def parseIdentStart(s: Stream, start: int) -> Result:
    if s[start] in "0123456789" or (s[start] in "+-" and s[start + 1] in "0123456789"):
        return Result.fail(start)
    if not isIdentChar(s[start]):
        return Result.fail(start)
    return Result(s[start], start + 1)


def parseNodeTerminator(s: Stream, start: int) -> Result:
    res = parseNewline(s, start)
    if res.valid:
        return res
    if s[start] == ";":
        return Result(";", start + 1)
    if s.eof(start):
        return Result(True, start)
    raise ParseError(s, start, f"Junk after node, before terminator.")


def parseEntity(s: Stream, start: int) -> Result:
    sd, i = parseSlashDash(s, start)

    ent, i = parseProperty(s, i)
    if ent is None:
        ent, i = parseValue(s, i)
        if ent is None:
            return Result.fail(start)
    ent.escaped = bool(sd)
    return Result(ent, i)


def parseProperty(s: Stream, start: int) -> Result:
    sd, i = parseSlashDash(s, start)

    key, i = parseIdent(s, i)
    if key is None:
        return Result.fail(start)
    if s[i] != "=":
        # property name might be a string,
        # so this isn't point-of-no-return yet
        return Result.fail(start)
    entity, i = parseValue(s, i + 1)
    if entity is None:
        raise ParseError(s, i, "Expected value after prop=.")
    entity.key = key
    return Result(entity, i)


def parseValue(s: Stream, start: int) -> Result:
    tag, i = parseTag(s, start)

    res, i = parseNumber(s, i)
    if res is not None:
        return Result(types.Entity(None, tag, res), i)

    res, i = parseKeyword(s, i)
    if res is not None:
        return Result(types.Entity(None, tag, res), i)

    res, i = parseString(s, i)
    if res is not None:
        return Result(types.Entity(None, tag, res), i)

    # Failed to find a value
    # But if I found a tag, something's up
    if tag:
        raise ParseError(s, start, "Found a tag, but no value following it.")
    return Result.fail(start)


def parseNumber(s: Stream, start: int) -> Result:
    if not isNumberStart(s, start):
        return Result.fail(start)
    res = parseBinaryNumber(s, start)
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
        s, start, f"Expected a number, but got junk after the initial digit."
    )


def parseBinaryNumber(s: Stream, start: int):
    i = start

    # optional sign
    sign, i = parseSign(s, i)
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "b"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isBinaryDigit(s[i]):
        raise ParseError(s, i, f"Expected binary digit after 0b, got junk.")

    # following digits/underscores
    end = i + 1
    while isBinaryDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 2) * sign
    return Result(types.Binary(value), end)


def parseOctalNumber(s: Stream, start: int):
    i = start

    # optional sign
    sign, i = parseSign(s, i)
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "o"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isOctalDigit(s[i]):
        raise ParseError(s, i, f"Expected octal digit after 0o, got junk.")

    # following digits/underscores
    end = i + 1
    while isOctalDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 8) * sign
    return Result(types.Octal(value), end)


def parseHexNumber(s: Stream, start: int):
    i = start

    # optional sign
    sign, i = parseSign(s, i)
    if sign is None:
        sign = 1

    # prefix
    if not (s[i] == "0" and s[i + 1] == "x"):
        return Result.fail(start)
    i += 2

    # initial digit
    if not isHexDigit(s[i]):
        raise ParseError(s, i, f"Expected hex digit after 0x, got junk.")

    # following digits/underscores
    end = i + 1
    while isHexDigit(s[end]) or s[end] == "_":
        end += 1
    value = int(s[i:end].replace("_", ""), 16) * sign
    return Result(types.Hex(value), end)


def parseDecimalNumber(s: Stream, start: int) -> Result:
    i = start

    # gonna parse the whole thing with Python,
    # so intermediate results aren't important

    # optional sign
    _, i = parseSign(s, i)

    # integer part
    _, i = parseDigits(s, i)

    if s[i] == ".":
        result, i = parseDigits(s, i + 1)
        if result is None:
            raise ParseError(s, i, "Expected digit after decimal point.")

    if s[i] in ("e", "E"):
        _, i = parseSign(s, i + 1)
        result, i = parseDigits(s, i)
        if result is None:
            raise ParseError(s, i, "Expected number after exponent.")

    chars = s[start:i].replace("_", "")
    value: Union[int, float]
    try:
        value = int(chars, 10)
    except ValueError:
        try:
            value = float(chars)
        except ValueError:
            raise ParseError(
                s, start, "Number-like string didn't actually parse as a number."
            )
    return Result(types.Decimal(value), i)


def parseDigits(s: Stream, start: int) -> Result:
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


def parseSign(s: Stream, start: int) -> Result:
    if s[start] == "+":
        return Result(1, start + 1)
    if s[start] == "-":
        return Result(-1, start + 1)
    return Result.fail(start)


def parseKeyword(s: Stream, start: int) -> Result:
    if s[start : start + 4] in ("true", "null") and not isIdentChar(s[start + 4]):
        return Result(types.Keyword(s[start : start + 4]), start + 4)
    if s[start : start + 5] == "false" and not isIdentChar(s[start + 5]):
        return Result(types.Keyword(s[start : start + 5]), start + 5)
    return Result.fail(start)


def parseString(s: Stream, start: int) -> Result:
    if s[start] == '"':
        return parseEscapedString(s, start)
    if s[start] == "r" and s[start + 1] in ("#", '"'):
        return parseRawString(s, start)
    return Result.fail(start)


def parseEscapedString(s: Stream, start: int) -> Result:
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
                s, start, "Hit EOF while looking for the end of the string"
            )
        ch, i = parseEscape(s, i)
        if ch is None:
            raise ParseError(s, i, "Invalid escape sequence in string")
        rawChars += ch

    return Result(types.EscapedString(rawChars), i + 1)


def parseEscape(s: Stream, start: int) -> Result:
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
                s, start, "Unicode escapes must surround their codepoint in {}"
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
                s, hexStart, "Unicode escapes can contain at most six digits"
            )
        hexValue = int(s[hexStart:i], 16)
        if hexValue > 0x10FFFF:
            raise ParseError(
                s, hexStart, "Maximum codepoint in a unicode escape is 0x10ffff"
            )
        return Result(chr(hexValue), i + 1)
    raise ParseError(s, start, "Invalid character escape")


def parseRawString(s: Stream, start: int) -> Result:
    if s[start] != "r":
        return Result.fail(start)
    i = start + 1

    # count hashes
    hashCount, i = parseInitialHashes(s, i)

    if s[i] != '"':
        return Result.fail(start)
    i = i + 1

    stringStart = i
    while True:
        while s[i] not in ('"', ""):
            i += 1
        if s[i] == "":
            raise ParseError(
                s, start, "Hit EOF while looking for the end of the raw string."
            )
        stringEnd = i
        i += 1
        result, i = parseFinalHashes(s, i, expectedCount=hashCount)
        if result:
            return Result(types.RawString(s[stringStart:stringEnd]), i)


def parseInitialHashes(s: Stream, start: int) -> Result:
    i = start
    while s[i] == "#":
        i += 1
    if s[i] == '"':
        return Result(i - start, i)
    return Result.fail(start)


def parseFinalHashes(s: Stream, start: int, expectedCount: int) -> Result:
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


def parseNewline(s: Stream, start: int) -> Result:
    if s[start] == "\x0d" and s[start + 1] == "\x0a":
        return Result(True, start + 2)
    if isNewlineChar(s[start]):
        return Result(True, start + 1)
    return Result.fail(start)


def parseLinespace(s: Stream, start: int) -> Result:
    if not isLinespaceChar(s[start]):
        return Result.fail(start)
    end = start + 1
    while isLinespaceChar(s[end]):
        end += 1
    return Result(True, end)


def parseNodespace(s: Stream, start: int) -> Result:
    i = start
    while True:
        _, i = parseWhitespace(s, i)
        escline, i = parseEscline(s, i)
        if escline is None:
            return Result(True, i)


def parseEscline(s: Stream, start: int) -> Result:
    if s[start] != "\\":
        return Result.fail(start)
    _, i = parseWhitespace(s, start + 1)
    if s[i] != "\n":
        return Result.fail(start)
    return Result(True, i + 1)


def parseWhitespace(s: Stream, start: int) -> Result:
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


def parseSlashDash(s: Stream, start: int) -> Result:
    if s[start] == "/" and s[start + 1] == "-":
        _, i = parseNodespace(s, start + 2)
        return Result(True, i)
    return Result.fail(start)
