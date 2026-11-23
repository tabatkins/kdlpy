from __future__ import annotations

from pathlib import Path

import pytest

import kdl

TESTS_DIR = Path(__file__).resolve().parent.parent / "upstream_tests" / "test_cases"
INPUT_DIR = TESTS_DIR / "input"
GOLDEN_DIR = TESTS_DIR / "expected_kdl"

_INPUT_FILES = sorted(p.name for p in INPUT_DIR.glob("*.kdl"))
_GOLDEN_FILES = {p.name for p in GOLDEN_DIR.glob("*.kdl")}

_PARSER = kdl.Parser(
    parseConfig=kdl.ParseConfig(
        nativeUntaggedValues=False,
        version=2,
    ),
    printConfig=kdl.PrintConfig(
        indent="    ",
        respectRadix=False,
        respectStringType=False,
        exponent="E",
        sortProperties=True,
    ),
)


def test_upstream_corpus_present() -> None:
    assert _INPUT_FILES, f"No upstream KDL2 test files found in {INPUT_DIR}"


@pytest.mark.parametrize("filename", _INPUT_FILES)
def test_kdl2_upstream(filename: str) -> None:
    inputText = (INPUT_DIR / filename).read_text(encoding="utf-8")
    if filename not in _GOLDEN_FILES:
        with pytest.raises(kdl.ParseError):
            _PARSER.parse(inputText)
        return

    goldenText = (GOLDEN_DIR / filename).read_text(encoding="utf-8")
    doc = _PARSER.parse(inputText)
    assert doc.print() == goldenText
