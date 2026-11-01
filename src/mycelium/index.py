"""The org index: each session's org slice, checked for freshness before every
decision (hub plan U14; KTD1, KTD7, KTD18).

`refresh` runs the helper's `read --json` once. When every cluster's revision
matches the cached index, it reads the cache; otherwise it rebuilds. The
index lives in the XDG cache and can be wiped at any time. What a wipe must
not lose lives in state (R28):

- **last-good snapshots**, one per cluster that validated, so a cluster that
  later fails keeps its last good entry, marked stale (KTD18);
- **hold records** for names that left every cluster, with their last-known
  manager, kept 7 days (KTD7). Ending a hold is `sweep`'s job, not this one's.

A validation failure never makes a name gone; a registered home that is gone,
or holds no cluster.toml, does.
"""

import fcntl
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mycelium import clusters
from mycelium.paths import Paths
from mycelium.topology import Declared, Slice, build, unknown

HOLD_SECONDS = 7 * 24 * 60 * 60
# How long a caller waits for another process's rebuild before building in
# memory: a hook must finish within its harness's timeout.
LOCK_WAIT_SECONDS = 1.0
FORMAT = 1  # of the cache, snapshot and hold files

HAND_EDITED = (
    "cluster.toml was edited by hand: its revision doesn't match its content."
    " Adopt it through the cluster helper"
)


class IndexUnavailable(Exception):
    """The index's own files can't be used: damaged hold records, or a symlink
    where mycelium's state or cache folder should be."""


@dataclass(frozen=True)
class Hold:
    """A name that left every cluster (KTD7)."""

    name: str
    removed_at: float
    manager: str | None
    """The last-known manager, from the graph before the name was removed."""
    told: tuple[str, ...] = ()

    def ended(self, now: float) -> bool:
        return now >= self.removed_at + HOLD_SECONDS

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "removed_at": self.removed_at,
            "manager": self.manager,
            "told": list(self.told),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Hold":
        name, removed_at, manager, told = (
            data["name"],
            data["removed_at"],
            data["manager"],
            data["told"],
        )
        if not isinstance(name, str) or not isinstance(removed_at, int | float):
            raise ValueError("a hold needs a name and a removal time")
        if manager is not None and not isinstance(manager, str):
            raise ValueError(f"the hold on {name} has a manager that isn't a name")
        if not isinstance(told, list) or not all(isinstance(item, str) for item in told):
            raise ValueError(f"the hold on {name} has a told list that isn't names")
        return cls(name, float(removed_at), manager, tuple(told))


@dataclass(frozen=True)
class Change:
    """What a rebuild changed, for the org events U6 emits."""

    kind: str
    """`added`, `held` or `manager`."""
    name: str
    old: str | None = None
    new: str | None = None


class Index:
    def __init__(
        self,
        slices: Mapping[str, Slice],
        errors: Mapping[str, tuple[str, ...]],
        holds: Mapping[str, Hold],
    ) -> None:
        self._slices = dict(slices)
        self._errors = dict(errors)
        self._holds = dict(holds)

    def names(self) -> frozenset[str]:
        """Every routable name: declared in a cluster, stale or not."""
        return frozenset(self._slices)

    def slice(self, name: str) -> Slice | None:
        return self._slices.get(name)

    def manager(self, name: str) -> str | None:
        found = self._slices.get(name)
        return None if found is None else found.manager

    def run(self, name: str) -> str | None:
        found = self._slices.get(name)
        return None if found is None else found.run

    def holds(self) -> tuple[Hold, ...]:
        return tuple(self._holds[name] for name in sorted(self._holds))

    def hold(self, name: str) -> Hold | None:
        return self._holds.get(name)

    def errors(self) -> dict[str, tuple[str, ...]]:
        """Validation failures, by cluster home."""
        return dict(self._errors)


@dataclass(frozen=True)
class Refresh:
    index: Index
    rebuilt: bool
    changes: tuple[Change, ...]


Reader = Callable[[], list[clusters.ClusterRecord]]


def refresh(
    paths: Paths,
    read: Reader = clusters.read,
    now: float | None = None,
    lock_wait: float = LOCK_WAIT_SECONDS,
) -> Refresh:
    """The index as the clusters stand now, after one revision check (KTD1).

    Raises clusters.HelperUnavailable when the helper can't answer, and
    IndexUnavailable when the index's own state can't be used: the caller says
    so plainly and never infers "no manager" (R23)."""
    now = time.time() if now is None else now
    read_at = time.time()
    records = _distinct(read())
    key = _key(records)
    files = _Files(paths)
    cached = files.cached(key)
    if cached is not None:
        return Refresh(cached, False, ())
    if not records and not files.has_state():
        return Refresh(Index({}, {}, {}), False, ())  # no clusters: write nothing (KTD13)
    with files.locked(lock_wait) as held:
        # Another process may have rebuilt while we waited, from this answer or a later one.
        cached = files.cached(key, newer_than=read_at)
        if cached is not None:
            return Refresh(cached, False, ())
        # Without the lock, build in memory only: the holder writes, and reports the changes.
        return _rebuild(files, records, key, now, read_at, write=held)


def _distinct(records: list[clusters.ClusterRecord]) -> list[clusters.ClusterRecord]:
    """One record per cluster: a home registered under two paths counts once."""
    seen: set[str] = set()
    kept = []
    for record in records:
        home = os.path.realpath(record.home)
        if home not in seen:
            seen.add(home)
            kept.append(record)
    return kept


def _key(records: list[clusters.ClusterRecord]) -> list[list[Any]]:
    """What decides a rebuild: each cluster's home, status, reason and revisions."""
    return sorted(
        [
            os.path.realpath(record.home),
            record.status,
            record.reason,
            record.revision,
            record.stored_revision,
            None if record.instance is None else record.instance.suffix,
        ]
        for record in records
    )


def _problem(record: clusters.ClusterRecord) -> str | None:
    if record.status == "unreadable":
        return record.reason or "cluster.toml can't be read"
    if record.stored_revision != record.revision:
        return HAND_EDITED
    return None


def _rebuild(
    files: "_Files",
    records: list[clusters.ClusterRecord],
    key,
    now: float,
    read_at: float,
    write: bool,
) -> Refresh:
    snapshots = files.snapshots()
    held_before = files.holds()
    holds = dict(held_before)
    present = [
        (os.path.realpath(record.home), record) for record in records if record.status != "missing"
    ]
    failing: dict[str, list[str]] = {}
    inputs = []
    opaque = set()  # failing, unreadable and never good: nothing is known of them
    current: dict[str, Declared] = {}
    for home, record in present:
        suffix = None if record.instance is None else record.instance.suffix
        if record.status == "ok":
            current[home] = Declared(home, suffix, record.declaration or {})
        problem = _problem(record)
        if problem is None:
            inputs.append(current[home])
            continue
        failing[home] = [problem]
        # Its pointers still place the clusters below it, and its names stay known.
        if home in snapshots:
            snapshot = snapshots[home]
            inputs.append(Declared(home, snapshot["suffix"], snapshot["declaration"]))
        elif home in current:
            inputs.append(current[home])
        else:
            opaque.add(home)

    graph = build(inputs, opaque=opaque)
    for home, reasons in graph.errors.items():
        failing.setdefault(home, []).extend(reasons)

    slices: dict[str, Slice] = {}
    kept: dict[str, dict[str, Any]] = {}
    for home, record in present:
        if home in failing:
            reason = "; ".join(failing[home])
            if home in snapshots:
                kept[home] = snapshots[home]
                for data in snapshots[home]["slices"]:
                    slices[data["name"]] = _stale(Slice.from_json(data), reason)
            elif home in current:
                for found in unknown(current[home], reason):
                    slices[found.name] = found
            continue
        above = _failing_above(home, graph.parent, failing)
        made = [graph.slices[name] for name in graph.names.get(home, [])]
        for found in made:
            if above is None and found.orphan and opaque:
                above = "a cluster that can't be read may point at this one: " + ", ".join(
                    sorted(opaque)
                )
            slices[found.name] = found if above is None else _stale(found, above)
        kept[home] = {
            "format": FORMAT,
            "home": home,
            "revision": record.revision,
            "suffix": current[home].suffix,
            "declaration": current[home].declaration,
            "slices": [found.to_json() for found in made],
        }

    changes = _changes(snapshots, slices, holds, now)
    errors = {home: tuple(reasons) for home, reasons in failing.items()}
    if not write:
        return Refresh(Index(slices, errors, holds), True, ())
    files.write(kept, snapshots, holds, held_before, key, read_at, slices, errors)
    return Refresh(Index(slices, errors, holds), True, tuple(changes))


def _stale(found: Slice, reason: str) -> Slice:
    return replace(found, stale=reason)


def _failing_above(home: str, parent: Mapping[str, str], failing: Mapping[str, list[str]]):
    seen = {home}
    above = parent.get(home)
    while above is not None and above not in seen:
        if above in failing:
            return f"a cluster above this one fails validation: {'; '.join(failing[above])}"
        seen.add(above)
        above = parent.get(above)
    return None


def _changes(snapshots, slices: Mapping[str, Slice], holds: dict[str, Hold], now: float):
    """Holds made and dropped in `holds`, and what changed since the snapshots."""
    before = {data["name"]: data for snapshot in snapshots.values() for data in snapshot["slices"]}
    changes = []
    for name in sorted(set(before) - set(slices)):
        if name not in holds:
            holds[name] = Hold(name, now, before[name]["manager"])
            changes.append(Change("held", name, old=before[name]["manager"]))
    for name in list(holds):
        if name in slices:
            del holds[name]
    for name in sorted(slices):
        found = slices[name]
        if name not in before:
            changes.append(Change("added", name, new=found.manager))
        elif found.known and before[name]["manager"] != found.manager:
            changes.append(Change("manager", name, old=before[name]["manager"], new=found.manager))
    return changes


class _Files:
    """The index's files: a cache that can be wiped, and the state that can't."""

    def __init__(self, paths: Paths) -> None:
        self.cache = paths.cache / "index.json"
        self.state = paths.state / "index"
        self.snapshot_dir = self.state / "last-good"
        self.holds_file = self.state / "holds.json"
        self.lock_file = self.state / "lock"

    def has_state(self) -> bool:
        return self.holds_file.exists() or self.snapshot_dir.exists()

    @contextmanager
    def locked(self, wait: float) -> Iterator[bool]:
        """Hold the rebuild lock, waiting at most `wait` seconds; yields whether it is held."""
        _make_dirs(self.state)
        try:
            descriptor = os.open(self.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise IndexUnavailable(f"{self.lock_file} can't be opened: {error}") from None
        try:
            yield _take(descriptor, time.monotonic() + wait)
        finally:
            os.close(descriptor)

    def cached(self, key, newer_than: float | None = None) -> Index | None:
        """The cached index, when it was built from this answer of the helper's,
        or (with `newer_than`) from one read later than that time."""
        data = _load(self.cache)
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return None
        later = newer_than is not None and _is_number(data.get("read_at"))
        if data.get("key") != key and not (later and data["read_at"] > newer_than):
            return None
        try:
            slices = {name: Slice.from_json(item) for name, item in data["slices"].items()}
            errors = {home: tuple(reasons) for home, reasons in data["errors"].items()}
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return Index(slices, errors, self.holds())

    def snapshots(self) -> dict[str, dict[str, Any]]:
        found = {}
        if self.snapshot_dir.is_dir():
            for path in sorted(self.snapshot_dir.glob("*.json")):
                data = _load(path)
                if _is_snapshot(data):
                    found[data["home"]] = data
        return found

    def holds(self) -> dict[str, Hold]:
        """The hold records. A damaged file is reported, never read as "no holds"."""
        try:
            text = self.holds_file.read_text()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise IndexUnavailable(f"{self.holds_file} can't be read: {error}") from None
        try:
            data = json.loads(text)
            if data["format"] != FORMAT:
                raise ValueError(f"format {data['format']!r}")
            return {item["name"]: Hold.from_json(item) for item in data["holds"]}
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise IndexUnavailable(
                f"{self.holds_file} is damaged ({error!r}); mycelium won't guess who is on hold"
            ) from None

    def write(self, kept, before, holds, held_before, key, read_at, slices, errors) -> None:
        """Write what changed: `before` and `held_before` are what the rebuild read.

        Holds go first: a snapshot is removed only once the hold it gives rise
        to is written, so a crash in between can't lose the hold."""
        if holds != held_before or not self.holds_file.exists():
            listed = [holds[name].to_json() for name in sorted(holds)]
            _write(self.holds_file, {"format": FORMAT, "holds": listed})
        wanted = {_snapshot_name(home) for home in kept}
        for home, data in kept.items():
            if before.get(home) != data:
                _write(self.snapshot_dir / _snapshot_name(home), data)
        if self.snapshot_dir.is_dir():
            for path in self.snapshot_dir.glob("*.json"):
                if path.name not in wanted:
                    path.unlink()
        _write(
            self.cache,
            {
                "format": FORMAT,
                "read_at": read_at,
                "key": key,
                "slices": {name: found.to_json() for name, found in slices.items()},
                "errors": {home: list(reasons) for home, reasons in errors.items()},
            },
        )


def _take(descriptor: int, deadline: float) -> bool:
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_snapshot(data: Any) -> bool:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return False
    if not isinstance(data.get("home"), str) or not isinstance(data.get("declaration"), dict):
        return False
    if data.get("suffix") is not None and not isinstance(data["suffix"], str):
        return False
    try:
        for item in data["slices"]:
            Slice.from_json(item)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return True


def _snapshot_name(home: str) -> str:
    return hashlib.sha256(home.encode()).hexdigest()[:32] + ".json"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write(path: Path, data: Any) -> None:
    """Replace `path` whole: a reader sees the old file or the new one, never part."""
    _make_dirs(path.parent)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(data, handle, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(temporary)
        raise


def _make_dirs(folder: Path) -> None:
    """Create `folder` and its missing parents, owner-only.

    Like the store, the index never follows a symlink below the XDG folder:
    one anywhere in the path, dangling or not, is refused before anything is
    created through it."""
    existing = folder
    while not os.path.lexists(existing):
        existing = existing.parent
    if existing.resolve() != existing or not existing.is_dir():
        raise IndexUnavailable(
            f"{folder} is not canonical (a symlink?); mycelium does not follow one"
        )
    missing = []
    while folder != existing:
        missing.append(folder)
        folder = folder.parent
    for new in reversed(missing):
        try:
            new.mkdir(mode=0o700)
        except FileExistsError:
            continue  # another process made it a moment ago
        new.chmod(0o700)
