#!/usr/bin/env python

import sys
import os


import kdl

THIS_DIR = os.path.dirname(os.path.realpath(__file__))
TEST_DIR = os.path.join(THIS_DIR, "test_cases", "input")
GOLDEN_DIR = os.path.join(THIS_DIR, "test_cases", "expected_kdl")


def main():
    inputs, goldens = findTestFiles()
    good = []
    bad = []
    for filename in sorted(inputs):
        inputPath = os.path.join(TEST_DIR, filename)
        print(f"Testing {os.path.basename(filename)}... ", end=None)
        try:
            with open(inputPath, "r", encoding="utf-8") as fh:
                output = kdl.parse(fh.read()).print()
        except kdl.errors.ParseError as e:
            if filename not in goldens:
                # Success, parse failure was intended
                good.append(filename)
                print("Success!")
            else:
                # Whoops, incorrect parse failure.
                bad.append(filename)
                print("Unexpected parse failure :(")
                print(e)
                print("================")
            continue
        except Exception as e:
            # Whoops, internal error
            bad.append(filename)
            print("Internal error :(")
            print(e)
            print("================")
            continue
        # Successful parse!
        if filename not in goldens:
            # ...but it shoudln't have
            bad.append(filename)
            print("Unexpected successful parse. Got:")
            print(output)
            print("================")
            continue
        goldenPath = os.path.join(GOLDEN_DIR, filename)
        with open(goldenPath, "r", encoding="utf-8") as fh:
            golden = fh.read()
        if output == golden:
            good.append(filename)
            print("Success!")
            continue
        bad.append(filename)
        print("Output didn't match golden.")
        print("Got:")
        print(output)
        print("Expected:")
        print(golden)
        print("================")

    print("================")
    if not bad:
        print(f"Success, {len(good)}/{len(inputs)} tests passed.")
    else:
        print(f"Failure, {len(good)}/{len(inputs)} tests passed.")
        # for badFilename in bad:
        #    print(f"  * {badFilename}")


def findTestFiles():
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
