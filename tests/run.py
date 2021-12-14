#!/usr/bin/env python

import sys
import os
import argparse

from typing import Tuple, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import kdl

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
TEST_DIR = os.path.join(THIS_DIR, "test_cases", "input")
GOLDEN_DIR = os.path.join(THIS_DIR, "test_cases", "expected_kdl")


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("-v", dest="verbose", action="store_true")
    options = cli.parse_args()

    inputs, goldens = findTestFiles()
    good = []
    bad = []
    printConfig = kdl.PrintConfig(
        indent="    ",
        respectRadix=False,
        respectStringType=False,
        exponent="E",
        sortProperties=True,
    )
    parseConfig = kdl.ParseConfig(
        nativeUntaggedValues=False,
    )
    parser = kdl.Parser(parseConfig, printConfig)
    for filename in sorted(inputs):
        inputPath = os.path.join(TEST_DIR, filename)
        try:
            with open(inputPath, "r", encoding="utf-8") as fh:
                output = parser.parse(fh.read()).print()
        except kdl.ParseError as e:
            if filename not in goldens:
                # SUCCESS: expected parse failure
                good.append(filename)
            else:
                # FAILURE: unexpected parse failure
                bad.append(filename)
                if options.verbose:
                    print(f"Unexpected parse failure in {filename}")
                    print(e)
                    print("================")
            continue
        except Exception as e:
            raise
            # FAILURE: unexpected runtime error
            bad.append(filename)
            if options.verbose:
                print(f"BIG BAD: Internal error in {filename}")
                print(e)
                print("================")
            continue
        # Successful parse!
        if filename not in goldens:
            # FAILURE: successful parse, but should be a parse failure
            bad.append(filename)
            if options.verbose:
                print(f"Unexpected successful parse in {filename}. Got:")
                print(output)
                print("================")
            continue
        goldenPath = os.path.join(GOLDEN_DIR, filename)
        with open(goldenPath, "r", encoding="utf-8") as fh:
            golden = fh.read()
        if output == golden:
            # SUCCESS: successful parse, matched golden
            good.append(filename)
            continue
        bad.append(filename)
        if options.verbose:
            # FAILURE: successful parse, but didn't match golden
            print(f"Output didn't match golden in {filename}. Got:")
            print(output)
            print("Expected:")
            print(golden)
            print("================")

    if not bad:
        print(f"Success, {len(good)}/{len(inputs)} tests passed.")
    else:
        print(f"Failure, {len(good)}/{len(inputs)} tests passed.")
        # for badFilename in bad:
        #    print(f"  * {badFilename}")


def findTestFiles() -> Tuple[Set[str], Set[str]]:
    inputs = set()
    goldens = set()
    for root, _, filenames in os.walk(TEST_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                inputs.add(filename)
    for root, _, filenames in os.walk(GOLDEN_DIR):
        for filename in filenames:
            if os.path.splitext(filename)[1] == ".kdl":
                goldens.add(filename)
    return inputs, goldens


if __name__ == "__main__":
    main()
