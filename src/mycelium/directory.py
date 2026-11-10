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
    last_seen: str | None
    knock: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": self.declared,
            "deliverability": self.deliverability,
            "harness": self.harness,
            "generation": self.generation,
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
    names = set(index.names()) | {hold.name for hold in index.holds()} | set(by_name)
    return [_row(name, index, by_name.get(name, []), context, unhandled) for name in sorted(names)]


@contextmanager
def unhandled_in(store_path: Path) -> Iterator[Unhandled]:
    """Whether a name's mailbox holds unhandled mail, read from the message store.

    The store opens once, through `open_store` and its rules (a symlink or a
    newer schema is refused: StoreRefused), at the first question, and closes
    with the context. With no store file yet, nothing is unhandled and nothing
    is created (KTD13)."""
    opened: list[Store] = []

    def unhandled(name: str) -> bool:
        if not opened:
            if not os.path.lexists(store_path):
                return False
            opened.append(open_store(store_path))
        return opened[0].has_unhandled(name)

    try:
        yield unhandled
    finally:
        for store in opened:
            store.close()


def _row(
    name: str, index: Index, bound: list[Binding], context: Context, unhandled: Unhandled
) -> Row:
    live = next((b for b in bound if b.state == "current" and liveness(b, context) != "dead"), None)
    shown = live or _latest(b for b in bound if b.state == "current") or _latest(bound)
    declared = name in index.names()
    deliverability = _deliverability(name, declared, index, live, bound, context, unhandled)
    can = deliverability == "live"
    return Row(
        name=name,
        declared=declared,
        deliverability=deliverability,
        harness=shown.harness if shown else None,
        generation=shown.generation if shown else None,
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
    # Undeclared (R37): unbound since its last binding was last seen.
    if live:
        return "live"
    last = max((b.last_seen for b in bound), default=None)
    if last is not None and context.now - last >= UNDECLARED_GONE_SECONDS:
        if not unhandled(name):
            return "gone"
    return "queue-only"


def _latest(bound: Iterable[Binding]) -> Binding | None:
    return max(bound, key=lambda b: b.last_seen, default=None)
