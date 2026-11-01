"""A test-only stand-in for the cluster helper's `read --json` (cluster spec §2.4).

The helper isn't released yet. This script prints the shape mycelium's stub
expects (posted on GRO-48), reading a fixture registry and the clusters'
`cluster.toml` files with `tomllib`. TOML is parsed here, in the tests, and
never under `src/` (KTD1).

    python fake_cluster_helper.py read --json    # FAKE_CLUSTER_REGISTRY names the registry
    python fake_cluster_helper.py seal <cluster.toml>...

A registry is a text file with one home per line, relative to the registry's
own folder or absolute. `seal` rewrites a file's `revision` to the digest of
the rest of it, as a helper write would.
"""

import hashlib
import json
import os
import re
import sys
import tomllib
from pathlib import Path

REVISION_LINE = re.compile(r"^revision\s*=.*$\n?", re.MULTILINE)
SCHEMAS = {1}


def digest(text: str) -> str:
    """The revision of a cluster file: the digest of everything but its revision line."""
    rest = REVISION_LINE.sub("", text)
    return "sha256:" + hashlib.sha256(rest.encode()).hexdigest()


def seal(path: Path) -> None:
    text = path.read_text()
    line = f'revision = "{digest(text)}"\n'
    if REVISION_LINE.search(text):
        text = REVISION_LINE.sub(line, text, count=1)
    else:
        text = line + text
    path.write_text(text)


def homes(registry: Path) -> list[Path]:
    lines = [line.strip() for line in registry.read_text().splitlines()]
    return [registry.parent / line for line in lines if line and not line.startswith("#")]


def record(home: Path) -> dict:
    base = {
        "home": os.path.abspath(home),
        "status": "ok",
        "reason": None,
        "revision": None,
        "stored_revision": None,
        "instance": None,
        "declaration": None,
    }
    path = home / ".kodhama" / "org" / "cluster.toml"
    if not home.is_dir():
        return base | {"status": "missing", "reason": "the home no longer exists"}
    if not path.is_file():
        return base | {"status": "missing", "reason": "the home holds no .kodhama/org/cluster.toml"}
    text = path.read_text()
    try:
        declaration = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return base | {"status": "unreadable", "reason": f"cluster.toml does not parse: {error}"}
    if declaration.get("schema") not in SCHEMAS:
        schema = declaration.get("schema")
        reason = f"cluster.toml has schema {schema!r}, which this helper can't read"
        return base | {"status": "unreadable", "reason": reason}
    stored = declaration.pop("revision", None)
    instance_path = home / ".kodhama" / "org" / "local" / "instance.toml"
    instance = None
    if instance_path.is_file():
        found = tomllib.loads(instance_path.read_text())
        instance = {"id": found["id"], "suffix": found["suffix"]}
    return base | {
        "revision": digest(text),
        "stored_revision": stored,
        "instance": instance,
        "declaration": declaration,
    }


def main(argv: list[str]) -> int:
    if argv[:1] == ["seal"]:
        for name in argv[1:]:
            seal(Path(name))
        return 0
    if argv != ["read", "--json"]:
        print(f"unknown command: {' '.join(argv)}", file=sys.stderr)
        return 2
    registry = Path(os.environ["FAKE_CLUSTER_REGISTRY"])
    print(json.dumps({"clusters": [record(home) for home in homes(registry)]}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
