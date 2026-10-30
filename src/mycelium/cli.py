"""The mycelium command line."""

import argparse
from dataclasses import fields

from mycelium import paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mycelium")
    # Not argparse's "version" action, which exits instead of returning.
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("paths", help="print the folders mycelium uses; creates nothing")
    args = parser.parse_args(argv)
    if args.version:
        # Imported here: importlib.metadata costs a hook run tens of milliseconds.
        from importlib.metadata import version

        print(f"mycelium {version('mycelium')}")
        return 0
    if args.command == "paths":
        resolved = paths.resolve()
        for field in fields(resolved):
            print(field.name, getattr(resolved, field.name))
        return 0
    parser.print_help()
    return 0
