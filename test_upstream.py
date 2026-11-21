#!/usr/bin/env python

from __future__ import annotations

import argparse
import os
import pprint
import sys

# Force the local kdl module into the environment
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import kdl  # pylint: disable=wrong-import-position

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
TEST_DIR = os.path.join(THIS_DIR, "upstream_tests", "test_cases", "input")
GOLDEN_DIR = os.path.join(THIS_DIR, "upstream_tests", "test_cases", "expected_kdl")


def main() -> bool:
    cli = argparse.ArgumentParser()
    cli.add_argument("-v", dest="verbose", action="count", default=0)
    cli.add_argument("test", nargs="?", default=None, help="Run a single test, and get a diff.")
    options = cli.parse_args()

    printConfig = kdl.PrintConfig(
        indent="    ",
        respectRadix=False,
        respectStringType=False,
        exponent="E",
        sortProperties=True,
    )
    parseConfig = kdl.ParseConfig(
        nativeUntaggedValues=False,
        version=2,
    )
    parser = kdl.Parser(parseConfig, printConfig)

    if options.test:
        if options.test.endswith(".kdl"):
            singleTestName = options.test
        else:
            singleTestName = options.test + ".kdl"
        inputs = {singleTestName}
        if singleTestName.endswith("_fail.kdl"):
            goldens = set()
        else:
            goldens = {singleTestName}
    else:
        inputs, goldens = findTestFiles()

    good = []
    bad = []
    for filename in sorted(inputs):
        inputPath = os.path.join(TEST_DIR, filename)
        inputText: str
        with open(inputPath, "r", encoding="utf-8") as fh:
            inputText = fh.read()

        goldenText: str | None
        if filename in goldens:
            goldenPath = os.path.join(GOLDEN_DIR, filename)
            with open(goldenPath, "r", encoding="utf-8") as fh:
                goldenText = fh.read()
        else:
            goldenText = None

        try:
            outputDoc = parser.parse(inputText)
            outputText = outputDoc.print()
        except kdl.ParseError as e:
            if goldenText is None:
                # SUCCESS: expected parse failure
                good.append(filename)
            else:
                # FAILURE: unexpected parse failure
                bad.append(filename)
                if options.verbose >= 2:
                    print(f"Unexpected parse failure in {filename}")
                    print(e)
                    print("Input:")
                    print(inputText)
                    print("Expected:")
                    print(goldenText)
                    print("================")
            continue
        # Successful parse!
        if goldenText is None:
            # FAILURE: successful parse, but should be a parse failure
            bad.append(filename)
            if options.verbose >= 2:
                try:
                    print(f"Unexpected successful parse in {filename}.")
                    print("Input:")
                    print(inputText)
                    print("Unexpected output (should be a parse failure)")
                    print(outputText)
                    print("Doc:")
                    print(pprint.pformat(outputDoc))
                    print("================")
                except Exception as e:  # pylint: disable=broad-exception-caught
                    print("While attempting to report an unexpected successful")
                    print(f"parse in {filename}, encountered a Python error:")
                    print(e)
            continue
        if outputText == goldenText:
            # SUCCESS: successful parse, matched golden
            good.append(filename)
            continue
        bad.append(filename)
        if options.verbose >= 2:
            # FAILURE: successful parse, but didn't match golden
            print(f"Output didn't match golden in {filename}.")
            print("Input:")
            print(inputText)
            print("Expected:")
            print(goldenText)
            print("Got:")
            print(outputText)
            print("Doc:")
            print(pprint.pformat(outputDoc))
            print("================")

    if not bad:
        print(f"Success, {len(good)}/{len(inputs)} tests passed.")
        return True
    else:
        print(f"Failure, {len(good)}/{len(inputs)} tests passed.")
        if options.verbose >= 1:
            for badfilename in bad:
                print("* " + badfilename)
        return False


def findTestFiles() -> tuple[set[str], set[str]]:
    inputs = set()
    goldens = set()
    for _, _, filenames in os.walk(TEST_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                inputs.add(filename)
    for _, _, filenames in os.walk(GOLDEN_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                goldens.add(filename)
    return inputs, goldens


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
