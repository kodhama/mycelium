"""Bindings: which session holds which role (R1-R4, R35, R37; KTD3-KTD6; hub plan U4).

One row per (harness, session id), in an owner-only SQLite file in state
(`bindings.sqlite3`), under the message store's file rules. The hook (U8 for
Claude, U11 for Codex) calls `bind` at every SessionStart; the CLI calls
`adopt`, `resolve_caller` (for `whoami`) and `authorize`; the directory reads
`rows`. The rules are in docs/decisions/bindings.md. In short:

- **Idempotent on the key** (KTD4): a SessionStart that continues a session
  (its hook re-run in the same process, or its resume or compaction in any
  process of this boot) re-confirms the binding: it keeps its state,
  generation and nonce, takes the caller's pid and evidence, and passes the
  G2 check. Unbound sessions other than forks and nested ones, provisional
  ones, and a superseded one whose name its own process now holds are
  decided again instead.
- **Generations, per name**, rise when a binding becomes current by a fresh
  start, a `/clear` or a `/resume` onto another session in the same process,
  or an adoption. A resume or compaction of the same session keeps its own, and
  so does a session decided again under the same name that nobody else has held
  since; one that another session has held since, in this boot, stays out
  until it adopts (lead rulings, GRO-50 lead notes, 2026-11-10).
- **States:** `current`; `provisional` (a lease exists, its nonce not yet
  adopted); `superseded`; `unbound`, with the reason.
- **Supersede:** a binding that becomes current supersedes the name's other
  current or provisional ones, and any other session of its own process. `bind`
  and `adopt` return each one once, the moment it is superseded.
- **Liveness** is computed, never stored (KTD3): another boot's binding is
  dead; one with a pid is live while that pid runs under the harness's name;
  a pid-less one is live, unverified, for its boot until superseded.
"""

import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from mycelium import identity, lease
from mycelium.identity import Caller
from mycelium.launch import Launch
from mycelium.store import files
from mycelium.topology import MAINTAINER

SCHEMA_VERSION = 1
BUSY_TIMEOUT_SECONDS = 30
SOURCES = ("startup", "resume", "compact", "clear", "fork")
"""SessionStart sources, as Claude names them; U11 maps Codex's onto these."""

LAUNCH_NAMES_NOW_DISAGREE = "launch names now disagree"
FORK = "a fork binds unbound unless it adopts the lease nonce"
NESTED = "nested: another harness runs above this one; adopt the lease nonce to bind"
HELD_SINCE = "another session held {name} since; adopt the lease or start fresh to take it back"
_STAYS_UNBOUND = (FORK, NESTED)
"""The unbound reasons a SessionStart re-confirms; every other unbound session is decided again."""
_CONTINUES = ("resume", "compact")
"""The sources that continue the same session, even in a new process."""
NO_BINDING_YET = "no binding for this session yet: its SessionStart hook has not bound it"
_HOLDING = ("current", "provisional")
"""The states that hold a name: a new current binding supersedes them."""

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS bindings (
        harness TEXT NOT NULL,
        session_id TEXT NOT NULL,
        name TEXT,
        declared INTEGER NOT NULL,
        pid INTEGER,
        evidence TEXT NOT NULL,
        boot TEXT NOT NULL,
        generation INTEGER,
        state TEXT NOT NULL,
        reason TEXT,
        nonce TEXT,
        bound_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        seen_holds INTEGER,
        bound_seen REAL,
        PRIMARY KEY (harness, session_id)
    )""",
    "CREATE INDEX IF NOT EXISTS bindings_by_name ON bindings (name)",
    """CREATE TABLE IF NOT EXISTS generations (
        name TEXT PRIMARY KEY,
        generation INTEGER NOT NULL,
        holds INTEGER NOT NULL DEFAULT 0,
        held_boot TEXT
    )""",
)
_FIELDS = (
    "harness, session_id, name, declared, pid, evidence, boot, generation,"
    " state, reason, nonce, bound_at, last_seen, seen_holds, bound_seen"
)


class Refused(Exception):
    """The caller may not do this: it is not current, or the lease refuses it."""

    def __init__(self, reason: str, state: str = "unbound") -> None:
        super().__init__(reason)
        self.reason = reason
        self.state = state


@dataclass(frozen=True)
class Binding:
    harness: str
    session_id: str
    name: str | None
    declared: bool
    """False for a name no cluster declares (R37)."""
    pid: int | None
    """The harness process; None for a Codex TUI or a caller found by thread id."""
    evidence: str
    """What the identity rested on: `walk` or `codex-thread-id` (identity.Caller.source)."""
    boot: str
    generation: int | None
    """None until the binding is first current."""
    state: str
    reason: str | None
    nonce: str | None
    """The lease nonce it adopted, or inherited in its own process."""
    bound_at: float
    last_seen: float
    seen_holds: int | None = None
    """The name's hold count when the binding last held it or went provisional for
    it; None if neither. Every binding that becomes current for the name raises
    the count, even one that keeps its generation, so nobody else has held the
    name since while the count still equals it (lead ruling, GRO-50)."""
    bound_seen: float | None = None
    """When the binding was last seen current; None if it never was. An undeclared
    name's 7-day clock runs from it, so a session that never holds the name, such
    as one with no launch name, never refreshes the clock (lead ruling, GRO-50)."""

    @property
    def key(self) -> tuple[str, str]:
        return self.harness, self.session_id

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "declared": self.declared,
            "state": self.state,
            "reason": self.reason,
            "generation": self.generation,
            "harness": self.harness,
            "session_id": self.session_id,
            "pid": self.pid,
            "evidence": self.evidence,
            "boot": self.boot,
            "nonce": self.nonce,
            "bound_at": iso(self.bound_at),
            "last_seen": iso(self.last_seen),
        }


@dataclass(frozen=True)
class Event:
    """One SessionStart, as the hook reports it."""

    source: str
    """One of SOURCES."""
    caller: Caller
    launch: Launch


@dataclass(frozen=True)
class Context:
    """The machine a decision is made on."""

    boot: str | None
    now: float
    declared: Callable[[str], bool]
    """Whether a cluster declares the name."""
    claim: Callable[[str], lease.Claim]
    """grove's claim for the name."""
    alive: Callable[[int, str], bool | None]
    """Whether the pid runs under the harness's name; None when that can't be told."""
    clock: Callable[[], float] | None = None
    """Reads the time again once the write lock is held, after a busy wait of up
    to BUSY_TIMEOUT_SECONDS; None keeps `now`."""

    def locked(self) -> "Context":
        """This context with the time read again, for use once the write lock is held."""
        return self if self.clock is None else replace(self, now=self.clock())


@dataclass(frozen=True)
class Bound:
    """What `bind` or `adopt` did."""

    binding: Binding | None
    """None when nothing could be bound: the caller has no harness or session, or no boot id."""
    superseded: tuple[Binding, ...] = ()
    """Bindings this call superseded; each is reported once (R4)."""
    reason: str | None = None


@dataclass(frozen=True)
class Resolution:
    """The caller's binding, as `whoami` reports it (KTD5)."""

    state: str
    binding: Binding | None
    reason: str | None


def open_bindings(path: Path) -> "Bindings":
    """Open the bindings at `path`, creating them owner-only. Raises StoreRefused."""
    return Bindings(path)


def liveness(binding: Binding, context: Context) -> str:
    """`live`, `unverified` or `dead` (KTD3)."""
    if context.boot is None or binding.boot != context.boot:
        return "dead"
    if binding.pid is None:
        return "unverified"
    found = context.alive(binding.pid, binding.harness)
    if found is None:
        return "unverified"
    return "live" if found else "dead"


def may_supersede(caller: Caller, held: Binding, held_liveness: str) -> bool:
    """Whether a new binding for `caller` may supersede `held` (lead ruling, GRO-50).

    A caller whose nesting is unknown (`nested` None: a Codex caller with no walk
    evidence) never supersedes a live binding the process walk verified, one with
    a known pid. It may supersede a pid-less or dead binding."""
    if caller.nested is not None or held_liveness == "dead":
        return True
    return held.pid is None


def reconfirm(binding: Binding, launch: Launch) -> tuple[str, str | None]:
    """The state a re-confirmed binding keeps (lessons G2; lead ruling, GRO-50).

    Only current and superseded bindings, and forks and nested ones, are
    re-confirmed: a provisional one is always decided again. A later
    SessionStart of a current session whose launch names now name someone else
    unbinds it (R3). A rename between SessionStarts goes unseen until then."""
    if binding.name is not None and binding.state == "current":
        if launch.disagrees_with(binding.name):
            return "unbound", LAUNCH_NAMES_NOW_DISAGREE
    return binding.state, binding.reason


def claim_reader(index: Any) -> Callable[[str], lease.Claim]:
    """`Context.claim` from the index: grove's claim sits in the name's own checkout,
    under its declared name (cluster spec §1; docs/decisions/bindings.md)."""

    def claim(name: str) -> lease.Claim:
        found = index.slice(name)
        if found is None:
            return lease.Claim("none", reason=f"no cluster declares {name}, so it has no lease")
        return lease.read(lease.claim_path(found.home, found.declared_name))

    return claim


def process_alive(
    table: Callable[[], Mapping[int, identity.Proc]] = identity.process_table,
) -> Callable[[int, str], bool | None]:
    """`Context.alive` from one process-table read, made when first asked."""
    cache: dict[str, Mapping[int, identity.Proc] | None] = {}

    def alive(pid: int, harness: str) -> bool | None:
        if "table" not in cache:
            try:
                cache["table"] = table()
            except identity.ProcessTableUnavailable:
                cache["table"] = None
        procs = cache["table"]
        if procs is None:
            return None
        found = procs.get(pid)
        return found is not None and found.name == harness

    return alive


class Bindings:
    def __init__(self, path: Path) -> None:
        self._db = files.open_database(
            path, schema=_SCHEMA, version=SCHEMA_VERSION, busy_timeout=BUSY_TIMEOUT_SECONDS
        )

    def close(self) -> None:
        self._db.close()

    def rows(self) -> list[Binding]:
        found = self._db.execute(f"SELECT {_FIELDS} FROM bindings ORDER BY bound_at, rowid")
        return [_binding(row) for row in found]

    def live_bindings(self, context: Context) -> list[Binding]:
        """Current bindings that are not dead: the ones that get delivery."""
        return [
            row
            for row in self.rows()
            if row.state == "current" and liveness(row, context) != "dead"
        ]

    def bind(self, event: Event, context: Context) -> Bound:
        caller = event.caller
        if event.source not in SOURCES:
            raise ValueError(f"unknown SessionStart source {event.source!r}")
        if event.launch.name == MAINTAINER:
            raise ValueError(f"{MAINTAINER} is never a launch name (R35)")
        if not caller.resolved:
            return Bound(None, reason=caller.reason)
        if caller.session_id is None:
            return Bound(None, reason=caller.reason or "the session id is unknown")
        if context.boot is None:
            return Bound(None, reason="the boot id can't be read, so nothing binds")
        with files.write(self._db):
            row = self._get(caller.harness, caller.session_id)
            if row is not None and self._continues(row, event, context):
                return self._reconfirm(row, event, context)
            return self._decide(row, event, context)

    def adopt(self, caller: Caller, nonce: str, context: Context) -> Bound:
        """Adopt the lease with its nonce (KTD6). Raises Refused."""
        if not caller.resolved or caller.session_id is None:
            raise Refused(caller.reason or "the session id is unknown")
        if context.boot is None:
            raise Refused("the boot id can't be read")
        with files.write(self._db):
            # A pending claim can expire during the wait for the lock.
            done = self._adopt(caller, nonce, context.locked())
        if isinstance(done, Refused):
            raise done  # after the commit: a refusal may unbind the caller
        return done

    def _adopt(self, caller: Caller, nonce: str, context: Context) -> "Bound | Refused":
        row = self._own(caller, context)
        if row.name is None:
            raise Refused(f"the session has no launch name: {row.reason}")
        claim = context.claim(row.name)
        if claim.state not in ("pending", "live"):
            why = claim.reason or "there is no claim"
            raise Refused(f"no lease for {row.name} to adopt: {why}")
        if claim.boot != context.boot:
            raise Refused("the claim is from an earlier boot")
        if claim.state == "pending" and (claim.expires or 0) <= context.now:
            raise Refused("the pending claim expired")
        if claim.nonce != nonce:
            if row.state == "provisional":
                reason = "nonce mismatch: the lease's nonce differs"
                self._put(replace(row, state="unbound", reason=reason, last_seen=context.now))
            return Refused("the claim's nonce does not match")
        if claim.state == "live" and not self._holds_session(claim, row):
            raise Refused("another session holds the live lease")
        if row.state == "current" and row.nonce == nonce:
            return Bound(self._put(replace(row, last_seen=context.now)))
        generation = self._next_generation(row.name)
        found = replace(
            row,
            state="current",
            reason=None,
            nonce=nonce,
            generation=generation,
            last_seen=context.now,
        )
        return self._make_current(found, also=self._same_process(caller, context))

    def resolve_caller(self, caller: Caller, context: Context) -> Resolution:
        """The caller's binding (KTD5): Claude by pid and session id, Codex by thread id."""
        if not caller.resolved:
            return Resolution("unbound", None, caller.reason)
        if caller.session_id is None:
            return Resolution("unbound", None, caller.reason or "the session id is unknown")
        row = self._get(caller.harness, caller.session_id)
        if row is None:
            return Resolution("unbound", None, NO_BINDING_YET)
        if caller.harness == "claude" and row.pid != caller.pid:
            why = f"the binding is another process's (pid {row.pid}, not {caller.pid})"
            return Resolution("unbound", None, why)
        if row.boot != context.boot:
            return Resolution("unbound", None, "the binding is from an earlier boot")
        return Resolution(row.state, row, row.reason)

    def touch(self, caller: Caller, context: Context) -> Resolution:
        """Refresh the caller's own binding's `last_seen`, and nothing else.

        U8's UserPromptSubmit hook calls it, so a session that runs for days
        without a SessionStart stays seen (R37). The binding is found as
        `resolve_caller` finds it; for a caller with none from this boot, it
        changes nothing and returns that unbound resolution."""
        with files.write(self._db):
            found = self.resolve_caller(caller, context)
            if found.binding is None:
                return found
            seen = self._put(replace(found.binding, last_seen=context.now))
        return Resolution(found.state, seen, found.reason)

    def authorize(self, caller: Caller, context: Context) -> Binding:
        """The caller's current binding. Raises Refused for any other caller (R4)."""
        found = self.resolve_caller(caller, context)
        if found.state != "current":
            raise Refused(f"{found.state}: {found.reason}", found.state)
        return found.binding

    # Re-confirming a session that continues (KTD4).

    def _continues(self, row: Binding, event: Event, context: Context) -> bool:
        """Whether this SessionStart continues the row's session, which keeps its binding.

        The same session in the same process (a hook re-run), or a resume or
        compaction of it in any process of this boot (herdr restores a pane with
        `claude --resume`). Re-decided instead: an earlier boot's row, an unbound
        session other than a fork or a nested one, a provisional one (its lease may
        have ended), and a superseded one whose name another session of the caller's
        own process now holds (a /resume back, lessons G3)."""
        caller = event.caller
        if row.boot != context.boot:
            return False
        if row.pid != caller.pid and event.source not in _CONTINUES:
            return False
        if row.state == "unbound":
            if row.reason == HELD_SINCE.format(name=row.name):
                # Kept out stays out in this boot, unless a new herdr name binds it fresh.
                return event.launch.name in (None, row.name)
            return row.reason in _STAYS_UNBOUND
        if row.state == "provisional":
            return False
        if row.state == "superseded":
            return not self._held_in_process(row, caller, context)
        return True

    def _reconfirm(self, row: Binding, event: Event, context: Context) -> Bound:
        """Keep the state, generation and nonce; take the caller's pid and evidence."""
        caller = event.caller
        state, reason = reconfirm(row, event.launch)
        found = replace(
            row,
            state=state,
            reason=reason,
            pid=caller.pid,
            evidence=caller.source,
            last_seen=context.now,
        )
        found = self._put(found)
        return Bound(found, self._supersede(self._same_process(caller, context), found))

    def _held_in_process(self, row: Binding, caller: Caller, context: Context) -> bool:
        """Whether a session of the caller's own process holds the row's name, current."""
        if caller.pid is None or row.name is None:
            return False
        return any(
            other.state == "current"
            and other.key != row.key
            and other.pid == caller.pid
            and other.boot == context.boot
            for other in self._named(row.name)
        )

    # The decision for a session that does not continue its binding.

    def _decide(self, row: Binding | None, event: Event, context: Context) -> Bound:
        caller, name = event.caller, event.launch.name
        own = self._same_process(caller, context)
        # A generation belongs to its name: a row decided under another name drops it.
        # With no launch name, the row keeps its own name, so a session that re-runs
        # its hook while its names disagree neither loses its generation nor forgets
        # who has held the name since (lead ruling, GRO-50).
        same = row is not None and row.name is not None and name in (None, row.name)
        decided_name = row.name if same else name
        # bound_at says since when the session is bound to this name.
        renamed = row is None or row.name != decided_name
        found = Binding(
            harness=caller.harness,
            session_id=caller.session_id,
            name=decided_name,
            declared=row.declared if same else bool(name) and context.declared(name),
            pid=caller.pid,
            evidence=caller.source,
            boot=context.boot,
            generation=row.generation if same else None,
            state="unbound",
            reason=None,
            nonce=row.nonce if same else None,  # an unbound row keeps the lease it adopted
            bound_at=context.now if renamed else row.bound_at,
            last_seen=context.now,
            seen_holds=row.seen_holds if same else None,
            bound_seen=row.bound_seen if same else None,
        )
        if name is None:
            found = replace(found, reason=event.launch.reason)
        elif caller.nested:
            found = replace(found, reason=NESTED)
        elif event.source == "fork":
            found = replace(found, reason=FORK)
        else:
            found = self._for_name(found, row, own, caller, context)
        if found.state == "current":
            return self._make_current(found, also=own)
        found = self._put(found)
        return Bound(found, self._supersede(own, found))

    def _for_name(
        self,
        found: Binding,
        row: Binding | None,
        own: list[Binding],
        caller: Caller,
        context: Context,
    ) -> Binding:
        counter, holds, held_boot = self._name_state(found.name)
        if held_boot == context.boot and self._held_since(row, found, holds, context.boot):
            # Another session holds the name now, and with it any lease it adopted.
            return replace(found, reason=HELD_SINCE.format(name=found.name), nonce=None)
        found = replace(found, nonce=None)  # decided afresh below, from the lease
        others = self._rivals(found)
        claim = context.claim(found.name)
        # The row itself counts: a session decided again keeps the lease it adopted.
        mine = [] if row is None or row.boot != context.boot else [replace(row, pid=caller.pid)]
        # Any running session of the name can hold the lease, an unbound one included;
        # a superseded one has given it up.
        named = [
            other
            for other in self._named(found.name)
            if other.key != found.key and other.state != "superseded"
        ]

        def runs(claimed: lease.Claim) -> bool:
            return any(
                liveness(other, context) != "dead"
                and (other.session_id == claimed.session or other.nonce == claimed.nonce)
                for other in [*named, *mine]
            )

        if lease.in_force(claim, context.boot, context.now, runs):
            heir = next(
                (
                    o
                    for o in [*mine, *own]
                    if o.nonce == claim.nonce and o.name == found.name and o.seen_holds == holds
                ),
                None,
            )
            if heir is None:
                reason = f"a lease holds {found.name}: adopt its nonce to become current"
                return replace(found, state="provisional", reason=reason, seen_holds=holds)
            # A /clear or /resume in the process that holds the lease keeps the lease.
            found = replace(found, nonce=claim.nonce)
        else:
            for other in others:
                held = liveness(other, context)
                if other.state == "current" and not may_supersede(caller, other, held):
                    reason = (
                        f"{found.name} is held by a session the process walk verified,"
                        " and this caller's nesting is unknown"
                    )
                    return replace(found, reason=reason)
        # The same session under the same name keeps its generation while nobody has
        # taken a newer one (KTD4; lead ruling, GRO-50); otherwise it takes the next.
        keeps = found.generation is not None and found.generation == counter
        generation = found.generation if keeps else self._next_generation(found.name)
        return replace(found, state="current", reason=None, generation=generation)

    @staticmethod
    def _held_since(row: Binding | None, found: Binding, holds: int, boot: str) -> bool:
        """Whether another binding became current for the name after this unbound or
        provisional session last held it or went provisional for it (lead ruling,
        GRO-50; KTD4). Such a session stays out until it adopts the lease or starts
        fresh: re-running its hook can't take the role back. The caller checks that
        the name was last held in this boot: an earlier boot's holder keeps nobody
        out, as a session that returns after a reboot starts fresh (lead overrule,
        GRO-50). A row from an earlier boot, in any state, stays out the same way
        once a session of this boot has held the name: after a reboot the first
        session restored holds the role (ce-doc-review, GRO-50)."""
        if row is None or row.name != found.name:
            return False
        if row.boot != boot:  # it never held the name, or held it before this holder
            return True
        return (
            row.state in ("unbound", "provisional")
            and found.seen_holds is not None
            and found.seen_holds != holds
        )

    def _make_current(self, found: Binding, also: Sequence[Binding] = ()) -> Bound:
        """Write `found` as the name's holder: raise the hold count, record the boot."""
        rivals = self._rivals(found)
        holds = self._name_state(found.name)[1] + 1
        self._db.execute(
            "UPDATE generations SET holds = ?, held_boot = ? WHERE name = ?",
            (holds, found.boot, found.name),
        )
        found = self._put(replace(found, seen_holds=holds))
        return Bound(found, self._supersede([*rivals, *also], found))

    def _rivals(self, found: Binding) -> list[Binding]:
        """The name's other bindings that hold it: current or provisional."""
        return [
            other
            for other in self._named(found.name)
            if other.key != found.key and other.state in _HOLDING
        ]

    def _supersede(self, rows: Sequence[Binding], by: Binding) -> tuple[Binding, ...]:
        done: dict[tuple[str, str], Binding] = {}
        for row in rows:
            if row.key == by.key or row.key in done or row.state == "superseded":
                continue
            generation = f" (generation {by.generation})" if by.generation is not None else ""
            reason = f"superseded by {by.harness} session {by.session_id}{generation}"
            old = replace(row, state="superseded", reason=reason)
            self._put(old)
            done[row.key] = old
        return tuple(done.values())

    def _same_process(self, caller: Caller, context: Context) -> list[Binding]:
        """Other sessions of the caller's own process: one process runs one session."""
        if caller.pid is None:
            return []
        found = self._db.execute(
            f"SELECT {_FIELDS} FROM bindings WHERE harness = ? AND pid = ? AND boot = ?"
            " AND session_id != ? AND state != 'superseded' ORDER BY bound_at, rowid",
            (caller.harness, caller.pid, context.boot, caller.session_id),
        )
        return [_binding(row) for row in found]

    def _holds_session(self, claim: lease.Claim, row: Binding) -> bool:
        """The live claim's session is this one, or another session of its process."""
        if claim.session in (None, row.session_id):
            return True
        holder = self._get(row.harness, claim.session)
        return (
            holder is not None
            and row.pid is not None
            and holder.pid == row.pid
            and holder.boot == row.boot
        )

    def _own(self, caller: Caller, context: Context) -> Binding:
        """The caller's binding in any state, as `resolve_caller` finds it. Raises Refused."""
        found = self.resolve_caller(caller, context)
        if found.binding is None:
            raise Refused(found.reason)
        return found.binding

    def _get(self, harness: str, session_id: str) -> Binding | None:
        found = self._db.execute(
            f"SELECT {_FIELDS} FROM bindings WHERE harness = ? AND session_id = ?",
            (harness, session_id),
        ).fetchone()
        return None if found is None else _binding(found)

    def _named(self, name: str) -> list[Binding]:
        found = self._db.execute(
            f"SELECT {_FIELDS} FROM bindings WHERE name = ? ORDER BY bound_at, rowid", (name,)
        )
        return [_binding(row) for row in found]

    def _put(self, row: Binding) -> Binding:
        """Write the row, and return it as written: a current row is seen bound now."""
        if row.state == "current":
            row = replace(row, bound_seen=row.last_seen)
        self._db.execute(
            f"INSERT INTO bindings ({_FIELDS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (harness, session_id) DO UPDATE SET"
            " name = excluded.name, declared = excluded.declared, pid = excluded.pid,"
            " evidence = excluded.evidence, boot = excluded.boot,"
            " generation = excluded.generation, state = excluded.state,"
            " reason = excluded.reason, nonce = excluded.nonce,"
            " bound_at = excluded.bound_at, last_seen = excluded.last_seen,"
            " seen_holds = excluded.seen_holds, bound_seen = excluded.bound_seen",
            (
                row.harness,
                row.session_id,
                row.name,
                int(row.declared),
                row.pid,
                row.evidence,
                row.boot,
                row.generation,
                row.state,
                row.reason,
                row.nonce,
                row.bound_at,
                row.last_seen,
                row.seen_holds,
                row.bound_seen,
            ),
        )
        return row

    def _generation(self, name: str) -> int:
        return self._name_state(name)[0]

    def _name_state(self, name: str) -> tuple[int, int, str | None]:
        """The name's generation, its hold count, and the boot a binding last held it in."""
        found = self._db.execute(
            "SELECT generation, holds, held_boot FROM generations WHERE name = ?", (name,)
        ).fetchone()
        return (0, 0, None) if found is None else (found[0], found[1], found[2])

    def _next_generation(self, name: str) -> int:
        generation = self._generation(name) + 1
        self._db.execute(
            "INSERT INTO generations (name, generation) VALUES (?, ?)"
            " ON CONFLICT (name) DO UPDATE SET generation = excluded.generation",
            (name, generation),
        )
        return generation


def _binding(row: sqlite3.Row | tuple) -> Binding:
    values = list(row)
    values[3] = bool(values[3])
    return Binding(*values)


def iso(seconds: float) -> str:
    """UTC, to the second, as the JSON records give times."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
