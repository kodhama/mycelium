"""Where mycelium keeps its files, under the XDG folders (R28, cluster spec §7).

Resolving a path creates nothing: a folder appears only when a write needs it
(KTD13), so a fresh home or a cloud session is left untouched.

The paths are canonical: a symlinked home or XDG folder resolves to its
target, so one folder never compares unequal to itself (on macOS, /var is a
link to /private/var). Below the XDG folder, nothing may be a symlink: the
store refuses a path that is not canonical.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

SUITE = "kodhama"
TOOL = "mycelium"


@dataclass(frozen=True)
class Paths:
    state: Path
    """Bindings, hold records, last-good snapshots and audit cursors."""
    data: Path
    """Mailboxes and questions."""
    cache: Path
    """The index built from the clusters, which can be wiped at any time."""
    clusters: Path
    """The registry of cluster homes and the helper's lock state, shared by the suite."""

    @property
    def store(self) -> Path:
        return self.data / "store.sqlite3"


def resolve(env: Mapping[str, str] | None = None, home: Path | None = None) -> Paths:
    """The folders for this environment; `env` and `home` default to the process's own."""
    env = os.environ if env is None else env
    home = _canonical(Path.home() if home is None else home)
    state = _base(env, "XDG_STATE_HOME", home / ".local" / "state")
    return Paths(
        state=state / SUITE / TOOL,
        data=_base(env, "XDG_DATA_HOME", home / ".local" / "share") / SUITE / TOOL,
        cache=_base(env, "XDG_CACHE_HOME", home / ".cache") / SUITE / TOOL,
        clusters=state / SUITE / "clusters",
    )


def _base(env: Mapping[str, str], name: str, default: Path) -> Path:
    # The XDG Base Directory spec ignores an empty or relative value.
    value = env.get(name, "")
    return _canonical(Path(value) if os.path.isabs(value) else default)


def _canonical(path: Path) -> Path:
    # Non-strict: resolves the part that exists and keeps the rest as written.
    return path.resolve()
