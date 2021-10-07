def cli() -> None:
    import kdl
    import argparse
    import sys

    cli = argparse.ArgumentParser(description="KDL parser")
    cli.add_argument(
        "infile",
        nargs="?",
        type=argparse.FileType("r", encoding="utf-8"),
        default=sys.stdin,
    )
    cli.add_argument(
        "outfile",
        nargs="?",
        type=argparse.FileType("w", encoding="utf-8"),
        default=sys.stdout,
    )
    options = cli.parse_args()

    with options.infile as fh:
        doc = kdl.parse(fh.read())
    with options.outfile as fh:
        fh.write(doc.print())
