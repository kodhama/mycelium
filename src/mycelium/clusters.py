"""The one boundary to the cluster helper: `kodhama-cluster read --json` (cluster spec §2.4).

mycelium reads clusters only through this module, and never parses TOML
itself (KTD1). The helper isn't released yet, so the shape checked here is the
stub posted on GRO-48: one record per registered cluster, with the revision
the helper computes now, the one stored in the file, the checkout's instance
(§3), and the declaration as data (§2, §2.1). When the real helper ships, this
is the only module that changes.
"""

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

COMMAND = ("kodhama-cluster", "read", "--json")

# A hook runs this once per decision (KTD1), inside its harness's hook timeout.
TIMEOUT_SECONDS = 5.0

STATUSES = ("ok", "missing", "unreadable")


class HelperUnavailable(Exception):
    """The helper could not be run, or its answer was not the expected shape."""


@dataclass(frozen=True)
class Instance:
    id: str
    suffix: str


@dataclass(frozen=True)
class ClusterRecord:
    home: str
    status: str
    """`ok`; `missing` (the home is gone, or holds no cluster.toml); or `unreadable`."""
    reason: str | None
    revision: str | None
    """The digest the helper computed from the file's content just now."""
    stored_revision: str | None
    """The `revision` written in the file; another value means it was hand-edited."""
    instance: Instance | None
    declaration: Mapping[str, Any] | None
    """cluster.toml as data, without its `revision` key."""


def read(
    argv: Sequence[str] = COMMAND,
    env: Mapping[str, str] | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> list[ClusterRecord]:
    """Every registered cluster, in one helper call. Raises HelperUnavailable."""
    name = argv[0]
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=None if env is None else {**os.environ, **env},
            check=False,
        )
    except FileNotFoundError:
        raise HelperUnavailable(f"the cluster helper ({name}) is not installed") from None
    except subprocess.TimeoutExpired:
        raise HelperUnavailable(f"the cluster helper did not answer within {timeout:g}s") from None
    if done.returncode != 0:
        said = done.stderr.strip().splitlines()
        detail = f": {said[-1]}" if said else ""
        raise HelperUnavailable(f"the cluster helper exited {done.returncode}{detail}")
    try:
        return _records(json.loads(done.stdout))
    except (ValueError, TypeError, KeyError) as error:
        raise HelperUnavailable(f"the cluster helper gave unexpected output: {error}") from None


def _records(answer: Any) -> list[ClusterRecord]:
    if not isinstance(answer, dict) or not isinstance(answer.get("clusters"), list):
        raise ValueError("no clusters list")
    return [_record(item) for item in answer["clusters"]]


def _record(item: Any) -> ClusterRecord:
    if not isinstance(item, dict):
        raise ValueError("a cluster is not an object")
    home = _text(item["home"], "home")
    status = item.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} for {home}")
    declaration = item.get("declaration")
    if status == "ok":
        _text(item.get("revision"), "revision")
        if not isinstance(declaration, dict):
            raise ValueError(f"no declaration for {home}")
    instance = item.get("instance")
    if instance is not None:
        if not isinstance(instance, dict):
            raise ValueError(f"instance of {home} is not an object")
        instance = Instance(
            id=_text(instance.get("id"), "instance id"),
            suffix=_text(instance.get("suffix"), "instance suffix"),
        )
    return ClusterRecord(
        home=home,
        status=status,
        reason=_optional_text(item.get("reason"), "reason"),
        revision=_optional_text(item.get("revision"), "revision"),
        stored_revision=_optional_text(item.get("stored_revision"), "stored_revision"),
        instance=instance,
        declaration=declaration if status == "ok" else None,
    )


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is not a non-empty string")
    return value


def _optional_text(value: Any, what: str) -> str | None:
    return None if value is None else _text(value, what)
