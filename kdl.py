#!/usr/bin/env python


def main():
    from kdl import parser
    import argparse
    import sys

    cli = argparse.ArgumentParser(description="KDL parser")
    cli.add_argument(
        "infile",
        nargs="?",
        type=argparse.FileType("r", encoding="uft-8"),
        default=sys.stdin,
    )
    cli.add_argument(
        "outfile",
        nargs="?",
        type=argparse.FileType("w", encoding="uft-8"),
        default=sys.stdout,
    )
    options = cli.parse_args()

    with options.infile as fh:
        nodes = parser.parse(fh.read())
    with options.outfile as fh:
        for node in nodes:
            fh.write(node.print())


if __name__ == "__main__":
    main()
