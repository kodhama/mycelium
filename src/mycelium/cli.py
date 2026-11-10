"""The mycelium command line.

Exit codes for `whoami`, `adopt` and `directory` (lead ruling, GRO-50):

- **0**: done; for `whoami` and `adopt`, the caller is current.
- **1**: refused: the caller is provisional, superseded or unbound, or the
  lease refused the adoption. The reason is on stderr, and stdout is empty.
- **2**: a usage error: an unknown command or bad arguments.
- **3**: mycelium could not tell: the bindings store, the state folder, the
  index or the cluster helper could not be read, or the boot id is unknown.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields

from mycelium import bindings, boot, clusters, identity, lease, paths
from mycelium.store import StoreRefused

REFUSED = 1
USAGE = 2
UNAVAILABLE = 3

# The cluster helper's read, as the index takes it; tests replace it.
read_clusters = clusters.read


class _Unavailable(Exception):
    """mycelium could not tell: exit 3."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mycelium")
    # Not argparse's "version" action, which exits instead of returning.
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("paths", help="print the folders mycelium uses; creates nothing")
    whoami = commands.add_parser("whoami", help="print the calling session's name and generation")
    whoami.add_argument("--json", action="store_true", help="print the full binding record")
    adopt = commands.add_parser("adopt", help="adopt grove's lease with its nonce")
    adopt.add_argument("nonce")
    commands.add_parser("directory", help="print every name's deliverability as JSON")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit:  # argparse exits 2 on a usage error, 0 for --help
        return exit.code if isinstance(exit.code, int) else USAGE
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
    if args.command in ("whoami", "adopt", "directory"):
        run = {"whoami": _whoami, "adopt": _adopt, "directory": _directory}[args.command]
        try:
            return run(args)
        except _Unavailable as error:
            print(f"mycelium {args.command}: {error}", file=sys.stderr)
            return UNAVAILABLE
    parser.print_help()
    return 0


def _whoami(args: argparse.Namespace) -> int:
    caller = _caller()
    found = _resolution(caller)
    if found.state != "current":
        print(f"mycelium whoami: {found.state}: {found.reason}", file=sys.stderr)
        return REFUSED
    if args.json:
        print(json.dumps(found.binding.to_json()))
    else:
        _print_binding(found.binding)
    return 0


def _adopt(args: argparse.Namespace) -> int:
    caller = _caller()
    resolved = paths.resolve()
    if not caller.resolved:
        print(f"mycelium adopt: unbound: {caller.reason}", file=sys.stderr)
        return REFUSED
    if not os.path.lexists(resolved.bindings):
        print(f"mycelium adopt: unbound: {bindings.NO_BINDING_YET}", file=sys.stderr)
        return REFUSED
    index = _index(resolved)
    with _opened(resolved) as store:
        try:
            # The superseded notices go out as mail with U5; adopt only reports success.
            done = store.adopt(caller, args.nonce, _context(claims=bindings.claim_reader(index)))
        except bindings.Refused as refused:
            print(f"mycelium adopt: {refused.reason}", file=sys.stderr)
            return REFUSED
    _print_binding(done.binding)
    return 0


def _directory(args: argparse.Namespace) -> int:
    from mycelium import directory

    resolved = paths.resolve()
    index = _index(resolved)
    rows: list[bindings.Binding] = []
    if os.path.lexists(resolved.bindings):
        with _opened(resolved) as store:
            rows = store.rows()
    try:
        listed = directory.rows(
            index, rows, _context(boot_required=False), directory.unhandled_in(resolved.store)
        )
    except (sqlite3.Error, OSError) as error:
        raise _Unavailable(f"the message store can't be read: {error}") from None
    print(json.dumps({"names": [row.to_json() for row in listed]}))
    return 0


def _print_binding(binding: bindings.Binding) -> None:
    # Line 1 is the name exactly: the helper compares it with KODHAMA_AGENT (spec §9e).
    print(binding.name)
    print(f"generation {binding.generation}")


def _caller() -> identity.Caller:
    # identity.process_table is looked up here, at the call, so a test can stand one in.
    return identity.resolve(table=identity.process_table)


def _resolution(caller: identity.Caller) -> bindings.Resolution:
    if not caller.resolved:
        return bindings.Resolution("unbound", None, caller.reason)
    resolved = paths.resolve()
    if not os.path.lexists(resolved.bindings):
        if os.path.lexists(resolved.state) and not os.path.isdir(resolved.state):
            raise _Unavailable(f"the state folder {resolved.state} is not a folder")
        return bindings.Resolution("unbound", None, bindings.NO_BINDING_YET)
    with _opened(resolved) as store:
        return store.resolve_caller(caller, _context())


@contextmanager
def _opened(resolved: paths.Paths) -> Iterator[bindings.Bindings]:
    """The bindings store, opened as a context; any failure to open is exit 3."""
    try:
        store = bindings.open_bindings(resolved.bindings)
    except (StoreRefused, sqlite3.Error, OSError) as error:
        raise _Unavailable(f"the bindings store can't be read: {error}") from None
    try:
        yield store
    finally:
        store.close()


def _context(
    claims: Callable[[str], lease.Claim] | None = None, boot_required: bool = True
) -> bindings.Context:
    found = boot.boot_id()
    if found is None and boot_required:
        raise _Unavailable("the boot id can't be read")
    return bindings.Context(
        boot=found,
        now=time.time(),
        declared=lambda name: False,
        claim=claims or (lambda name: lease.Claim("none")),
        alive=bindings.process_alive(identity.process_table),
    )


def _index(resolved: paths.Paths):
    from mycelium import index

    try:
        return index.refresh(resolved, read=read_clusters).index
    except clusters.HelperUnavailable as error:
        raise _Unavailable(f"the cluster helper can't be read: {error}") from None
    except index.IndexUnavailable as error:
        raise _Unavailable(f"the index can't be read: {error}") from None
