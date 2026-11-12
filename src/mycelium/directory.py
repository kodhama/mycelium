"""The directory: who is reachable (R36), with each name's deliverability (R5, R37; KTD3).

Deliverability is computed when asked, from the index's `run` field and the
bindings, and never stored:

- **live:** `run = running` (or an undeclared name) and a live current binding;
- **absent:** `run = running` and no live current binding. A binding from an
  earlier boot is dead, so a reboot makes a role absent, never gone;
- **queue-only:** `run = parked`, or an undeclared name with no live binding;
- **held:** the name left every cluster and its 7-day hold hasn't ended (KTD7);
- **gone:** `run = done`, a hold that ended, or an undeclared name unbound for
  7 days with nothing unhandled in its mailbox.

The names are every name the index declares, every held name, and every name
a binding carries. A provisional binding gives no delivery (KTD6), so it does
not make a name live. A name can be knocked only while live: Claude through
the SendMessage hint, Codex through its queue (KTD9).
"""

import os
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mycelium.bindings import Binding, Context, iso, liveness
from mycelium.index import Index
from mycelium.store import Store, open_store

DELIVERABILITY = ("live", "absent", "queue-only", "held", "gone")
UNDECLARED_GONE_SECONDS = 7 * 24 * 60 * 60
KNOCK = {"claude": "sendmessage-hint", "codex": "codex-queue"}

Unhandled = Callable[[str], bool]


@dataclass(frozen=True)
class Row:
    name: str
    declared: bool
    deliverability: str
    harness: str | None
    generation: int | None
    state: str | None
    """The shown binding's state: a binding with no launch name keeps its old
    name, and reads `unbound` here (lead ruling, GRO-50)."""
    last_seen: str | None
    knock: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": self.declared,
            "deliverability": self.deliverability,
            "harness": self.harness,
            "generation": self.generation,
            "state": self.state,
            "last_seen": self.last_seen,
            "knock": dict(self.knock),
        }


def rows(
    index: Index,
    bound: Iterable[Binding],
    context: Context,
    unhandled: Unhandled = lambda name: False,
) -> list[Row]:
    """One row per name, sorted by name."""
    by_name: dict[str, list[Binding]] = {}
    for binding in bound:
        if binding.name is not None:
            by_name.setdefault(binding.name, []).append(binding)
    declared = index.names()
    names = declared | {hold.name for hold in index.holds()} | set(by_name)
    return [
        _row(name, name in declared, index, by_name.get(name, []), context, unhandled)
        for name in sorted(names)
    ]


@contextmanager
def unhandled_in(store_path: Path) -> Iterator[Unhandled]:
    """Whether a name's mailbox holds unhandled mail, read from the message store.

    At the first question it looks for the store once: it opens it through
    `open_store` and its rules (a symlink or a newer schema is refused:
    StoreRefused), and closes it with the context. With no store file then,
    nothing is unhandled for the whole listing, and nothing is created (KTD13)."""
    found: dict[str, Store | None] = {}

    def unhandled(name: str) -> bool:
        if "store" not in found:
            found["store"] = open_store(store_path) if os.path.lexists(store_path) else None
        store = found["store"]
        return store is not None and store.has_unhandled(name)

    try:
        yield unhandled
    finally:
        if found.get("store") is not None:
            found["store"].close()


def _row(
    name: str,
    declared: bool,
    index: Index,
    bound: list[Binding],
    context: Context,
    unhandled: Unhandled,
) -> Row:
    live = next((b for b in bound if b.state == "current" and liveness(b, context) != "dead"), None)
    shown = live or _latest(b for b in bound if b.state == "current") or _latest(bound)
    deliverability = _deliverability(name, declared, index, live, bound, context, unhandled)
    can = deliverability == "live"
    return Row(
        name=name,
        declared=declared,
        deliverability=deliverability,
        harness=shown.harness if shown else None,
        generation=shown.generation if shown else None,
        state=shown.state if shown else None,
        last_seen=iso(max(b.last_seen for b in bound)) if bound else None,
        knock={"can": can, "how": KNOCK.get(shown.harness) if can and shown else None},
    )


def _deliverability(
    name: str,
    declared: bool,
    index: Index,
    live: Binding | None,
    bound: list[Binding],
    context: Context,
    unhandled: Unhandled,
) -> str:
    if declared:
        run = index.run(name)
        if run == "done":
            return "gone"
        if run == "parked":
            return "queue-only"
        return "live" if live else "absent"
    hold = index.hold(name)
    if hold is not None:
        return "gone" if hold.ended(context.now) else "held"
    # Undeclared (R37): unbound since a binding was last seen holding the name, or,
    # for a name never held, since it was first seen (lead ruling, GRO-50).
    if live:
        return "live"
    last = _clock_start(bound)
    if last is not None and context.now - last >= UNDECLARED_GONE_SECONDS:
        if not unhandled(name):
            return "gone"
    return "queue-only"


def _clock_start(bound: list[Binding]) -> float | None:
    """When an undeclared name was last seen held, or, if never, first seen (R37).

    A binding never current under the name doesn't move the clock, so a fork or
    a session with no launch name can't keep the name from going."""
    held = [b.bound_seen for b in bound if b.bound_seen is not None]
    if held:
        return max(held)
    return min((b.bound_at for b in bound), default=None)


def _latest(bound: Iterable[Binding]) -> Binding | None:
    return max(bound, key=lambda b: b.last_seen, default=None)
