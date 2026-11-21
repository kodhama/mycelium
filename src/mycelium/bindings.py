"""Bindings: which session holds which role (R1-R4, R35, R37; KTD3-KTD6; hub plan U4).

One row per (harness, session id), in an owner-only SQLite file in state
(`bindings.sqlite3`), under the message store's file rules. The hook (U8 for
Claude, U11 for Codex) calls `bind` at every SessionStart; the CLI calls
`adopt`, `resolve_caller` (for `whoami`) and `authorize`; the directory reads
`rows` and `names`, the one record per name that holds the name's own facts
(U4c KC1). The rules are in docs/decisions/bindings.md. In short:

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

SCHEMA_VERSION = 3
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
        pid INTEGER,
        evidence TEXT NOT NULL,
        boot TEXT NOT NULL,
        generation INTEGER,
        state TEXT NOT NULL,
        reason TEXT,
        bound_at REAL NOT NULL,
        last_seen REAL NOT NULL,
        seen_holds INTEGER,
        PRIMARY KEY (harness, session_id)
    )""",
    "CREATE INDEX IF NOT EXISTS bindings_by_name ON bindings (name)",
    """CREATE TABLE IF NOT EXISTS names (
        name TEXT PRIMARY KEY,
        declared INTEGER NOT NULL,
        generation INTEGER NOT NULL,
        epoch INTEGER NOT NULL,
        held_boot TEXT,
        bound_seen REAL,
        first_bound_at REAL NOT NULL,
        holder_harness TEXT,
        holder_session TEXT,
        nonce TEXT,
        lease_pid INTEGER
    )""",
)
_RESET = tuple(
    f"DROP TABLE IF EXISTS {table}" for table in ("bindings", "generations", "names", "sightings")
)
"""Every bindings table any schema version made, or U4 will make (`sightings`):
an older file starts empty (KC7).

Safe only while no released mycelium writes binding rows; the first release
that ships the hooks (hub U8) ends it, and later versions migrate or refuse."""
_COLUMNS = (
    "harness, session_id, name, pid, evidence, boot, generation,"
    " state, reason, bound_at, last_seen, seen_holds"
)
_FIELDS = (
    "b.harness, b.session_id, b.name, COALESCE(n.declared, 0), b.pid, b.evidence, b.boot,"
    " b.generation, b.state, b.reason,"
    " CASE WHEN n.holder_harness = b.harness AND n.holder_session = b.session_id"
    " THEN n.nonce END,"
    " b.bound_at, b.last_seen, b.seen_holds"
)
_ROWS = "bindings AS b LEFT JOIN names AS n ON n.name = b.name"
"""A session row with its name's `declared` and, for the name's holder, its nonce:
the name record holds both (KC9)."""
_RECORD = (
    "name, declared, generation, epoch, held_boot, bound_seen, first_bound_at,"
    " holder_harness, holder_session, nonce, lease_pid"
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
    """False for a name no cluster declares (R37); read from the name record."""
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
    """The name's lease nonce when this session is the name's holder, else None: the
    name record holds it (U4c KC9)."""
    bound_at: float
    last_seen: float
    seen_holds: int | None = None
    """The name's hold count (`NameRecord.epoch`) when the binding last held it or
    went provisional for it; None if neither. Every binding that becomes current
    for the name raises the count, even one that keeps its generation, so nobody
    else has held the name since while the count still equals it (lead ruling,
    GRO-50)."""

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
class NameRecord:
    """One record per name: the name's own facts, which no session row's write
    changes (U4c KC1). Records are never deleted, so generations never repeat."""

    name: str
    declared: bool
    """Whether a cluster declared the name at the last SessionStart that carried it."""
    generation: int
    """The last generation issued; 0 until a binding is first current."""
    epoch: int
    """The hold count: +1 every time a binding becomes current for the name."""
    held_boot: str | None
    """The boot a binding last became current for the name in."""
    bound_seen: float | None
    """When a current binding of the name was last written; None if none ever was.
    An undeclared name's 7-day clock runs from it (R37; lead ruling, GRO-50)."""
    first_bound_at: float
    """When the record was made: the first SessionStart that carried the name. The
    clock of a name nobody ever held runs from it."""
    holder: tuple[str, str] | None = None
    """(harness, session id) of the session that last became current for the name, or
    succeeded its holder with no launch name under its lease (KC4); None if none has."""
    nonce: str | None = None
    """The holder's lease nonce: set by adoption, kept by an heir, cleared when a session
    becomes current with no lease in force (I4)."""
    lease_pid: int | None = None
    """The superseded holder's pid, while a live claim carries the nonce: the lease stays
    in force while that pid runs (KC5(b2), F4)."""


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
            path,
            schema=_SCHEMA,
            version=SCHEMA_VERSION,
            busy_timeout=BUSY_TIMEOUT_SECONDS,
            reset=_RESET,
        )

    def close(self) -> None:
        self._db.close()

    def rows(self) -> list[Binding]:
        found = self._db.execute(f"SELECT {_FIELDS} FROM {_ROWS} ORDER BY b.bound_at, b.rowid")
        return [_binding(row) for row in found]

    def listing(self) -> tuple[list[Binding], list[NameRecord]]:
        """The rows and the name records, read in one snapshot, as the directory needs
        them: a bind that commits between two separate reads could pair old rows with
        newer records."""
        self._db.execute("BEGIN")
        try:
            return self.rows(), self.names()
        finally:
            # SQLite may already have rolled back (an I/O error): keep that error.
            if self._db.in_transaction:
                self._db.execute("COMMIT")

    def names(self) -> list[NameRecord]:
        """Every name record, by name. A name keeps its record after its rows move on."""
        found = self._db.execute(f"SELECT {_RECORD} FROM names ORDER BY name")
        return [_name_record(row) for row in found]

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
            context = context.locked()  # the busy wait may have taken a while (KC8)
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
        if claim.state == "live" and claim.session is None:
            raise Refused("the live claim names no session")  # A77
        if claim.state == "live" and not self._holds_session(claim, row):
            raise Refused("another session holds the live lease")
        if row.state == "current" and row.nonce == nonce:  # A78: the holder, adopting again
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
        return self._make_current(found, context, also=self._same_process(caller, context))

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
            context = context.locked()  # the busy wait may have taken a while (KC8)
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
            if self._held_in_process(row, caller, context):
                return False
            return not self._resumes_its_lease(row, event, context)
        return True

    def _reconfirm(self, row: Binding, event: Event, context: Context) -> Bound:
        """Keep the state, generation and nonce; take the caller's pid and evidence."""
        caller = event.caller
        state, reason = reconfirm(row, event.launch)
        declared = row.declared
        if row.name is not None and event.launch.name == row.name:
            declared = self._record(row.name, context)
            self._lapse(self._name(row.name), context.claim(row.name), context)
        found = replace(
            row,
            declared=declared,
            state=state,
            reason=reason,
            pid=caller.pid,
            evidence=caller.source,
            last_seen=context.now,
        )
        found = self._put(found)
        own = self._same_process(caller, context)
        return Bound(found, self._supersede(own, found, context))

    def _resumes_its_lease(self, row: Binding, event: Event, context: Context) -> bool:
        """KC6's third exception (F4): a superseded row still the holder of its name,
        seen with that launch name in the name's `lease_pid` while KC5(b2) keeps the
        lease in force there, is the /resume back in the process that superseded it,
        and is decided again."""
        if row.name is None or event.launch.name != row.name:
            return False
        record = self._name(row.name)
        if record is None or record.holder != row.key:
            return False
        if event.caller.pid is None or event.caller.pid != record.lease_pid:
            return False
        return self._lease_pid_keeps(record, row, context.claim(row.name), context)

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
        # A generation belongs to its name: a row decided under another name drops it.
        # With no launch name, the row keeps its own name, so a session that re-runs
        # its hook while its names disagree neither loses its generation nor forgets
        # who has held the name since (lead ruling, GRO-50).
        same = row is not None and row.name is not None and name in (None, row.name)
        decided_name = row.name if same else name
        # A SessionStart that carries the name makes its record, or rewrites `declared`.
        declared = row.declared if same else False
        if name is not None:
            declared = self._record(name, context)
        own = self._same_process(caller, context)  # read after the record's rewrite
        # bound_at says since when the session is bound to this name.
        renamed = row is None or row.name != decided_name
        found = Binding(
            harness=caller.harness,
            session_id=caller.session_id,
            name=decided_name,
            declared=declared,
            pid=caller.pid,
            evidence=caller.source,
            boot=context.boot,
            generation=row.generation if same else None,
            state="unbound",
            reason=None,
            nonce=None,  # the name record keeps the holder's nonce (KC9)
            bound_at=context.now if renamed else row.bound_at,
            last_seen=context.now,
            seen_holds=row.seen_holds if same else None,
        )
        if name is None:
            found = replace(found, reason=event.launch.reason)
            if not caller.nested and event.source != "fork" and caller.pid is not None:
                found = self._succeed_nameless(found, own, context)
        elif caller.nested:
            found = replace(found, reason=NESTED)
        elif event.source == "fork":
            found = replace(found, reason=FORK)
        else:
            found = self._for_name(found, row, own, caller, context)
        if found.state == "current":
            return self._make_current(found, context, also=own)
        found = self._put(found)
        return Bound(found, self._supersede(own, found, context))

    def _for_name(
        self,
        found: Binding,
        row: Binding | None,
        own: list[Binding],
        caller: Caller,
        context: Context,
    ) -> Binding:
        record = self._name(found.name)
        if record is None:
            raise RuntimeError(f"no name record for {found.name}")
        claim = context.claim(found.name)
        record = self._lapse(record, claim, context)
        holds = record.epoch
        if record.held_boot == context.boot and self._held_since(row, found, holds, context.boot):
            # Another session holds the name now, and with it any lease it adopted.
            return replace(found, reason=HELD_SINCE.format(name=found.name))
        # The caller's own row is judged with the caller's pid and the name being decided.
        mine = None
        if row is not None and row.boot == context.boot:
            mine = replace(row, pid=caller.pid, name=found.name)
        if self._in_force(record, claim, mine, context):
            if not self._heir(record, claim, row, mine, caller, context):
                reason = f"a lease holds {found.name}: adopt its nonce to become current"
                return replace(found, state="provisional", reason=reason, seen_holds=holds)
            # A /clear or /resume in the process that holds the lease keeps the lease.
            found = replace(found, nonce=claim.nonce)
        else:
            for other in self._rivals(found):
                held = liveness(other, context)
                if other.state == "current" and not may_supersede(caller, other, held):
                    reason = (
                        f"{found.name} is held by a session the process walk verified,"
                        " and this caller's nesting is unknown"
                    )
                    return replace(found, reason=reason)
        # The same session under the same name keeps its generation while nobody has
        # taken a newer one (KTD4; lead ruling, GRO-50); otherwise it takes the next. A
        # superseded holder back under its lease takes the next, as A21 does (F4).
        keeps = (
            found.generation is not None
            and found.generation == record.generation
            and row is not None
            and row.state != "superseded"
        )
        generation = found.generation if keeps else self._next_generation(found.name)
        return replace(found, state="current", reason=None, generation=generation)

    def _in_force(
        self, record: NameRecord, claim: lease.Claim, mine: Binding | None, context: Context
    ) -> bool:
        """KC5: whether a lease holds the name. A pending claim holds while unexpired. A
        live one holds through (a) a running, not superseded row of the name that the
        claim names, or (b) the name's holder, still pointing at the name or at none,
        with the claim's nonce: running and not superseded (b1), or superseded with its
        `lease_pid` running (b2). No other row's nonce counts (limit G4)."""

        def judged(other: Binding) -> Binding:
            return mine if mine is not None and other.key == mine.key else other

        def runs(claimed: lease.Claim) -> bool:
            named = [judged(other) for other in self._named(record.name)]
            if mine is not None and all(other.key != mine.key for other in named):
                named.append(mine)
            if any(
                other.state != "superseded"
                and other.session_id == claimed.session
                and liveness(other, context) != "dead"
                for other in named
            ):
                return True  # (a)
            holder = self._holder(record)
            if holder is None:
                return False
            holder = judged(holder)
            if holder.name not in (record.name, None) or record.nonce != claimed.nonce:
                return False
            if holder.state != "superseded":
                return liveness(holder, context) != "dead"  # (b1)
            return self._lease_pid_keeps(record, holder, claimed, context)  # (b2)

        return lease.in_force(claim, context.boot, context.now, runs)

    def _lease_pid_keeps(
        self, record: NameRecord, holder: Binding, claim: lease.Claim, context: Context
    ) -> bool:
        """KC5(b2): the superseded holder's lease holds while its `lease_pid` runs and a
        live claim of this boot carries the name's nonce. Nothing else is checked."""
        return (
            record.lease_pid is not None
            and holder.name in (record.name, None)
            and claim.state == "live"
            and claim.boot == context.boot
            and claim.nonce == record.nonce
            and liveness(replace(holder, pid=record.lease_pid), context) != "dead"
        )

    def _heir(
        self,
        record: NameRecord,
        claim: lease.Claim,
        row: Binding | None,
        mine: Binding | None,
        caller: Caller,
        context: Context,
    ) -> bool:
        """KC5's gate: under a lease, only the name's holder or its successor (KC4) with
        the claim's nonce becomes current."""
        if record.nonce is None or claim.nonce != record.nonce:
            return False
        holder = self._holder(record)
        if holder is None:
            return False
        if row is not None and holder.key == row.key:
            # A superseded holder is heir only back in its lease_pid (KC6, F4).
            return holder.state != "superseded" or caller.pid == record.lease_pid
        return self._succeeds(record, holder, row, caller, claim, context)

    def _succeeds(
        self,
        record: NameRecord,
        holder: Binding,
        row: Binding | None,
        caller: Caller,
        claim: lease.Claim,
        context: Context,
    ) -> bool:
        """KC4: process succession. The caller succeeds the name's holder when the holder
        runs in the caller's process and still points at the name or at none, or when
        the holder is superseded and the caller runs in its `lease_pid` under KC5(b2).
        Forks and nested sessions never succeed; a kept-out row never reaches here."""
        if caller.pid is None or caller.nested:
            return False
        if row is not None and row.boot == context.boot and row.reason in _STAYS_UNBOUND:
            return False
        if holder.name not in (record.name, None):
            return False  # it renamed away: the name's lease lapsed (X6)
        if holder.state == "superseded":
            return caller.pid == record.lease_pid and self._lease_pid_keeps(
                record, holder, claim, context
            )
        return (
            holder.harness == caller.harness
            and holder.pid == caller.pid
            and holder.boot == context.boot
        )

    def _succeed_nameless(
        self, found: Binding, own: Sequence[Binding], context: Context
    ) -> Binding:
        """KC4 with no launch name: under a lease in force whose nonce is the name's, the
        caller becomes the holder of the name its process's holder held, unbound and
        nameless, so the lease stays in force through it (limit F3). With no such lease
        nothing passes."""
        for other in own:
            for record in self._held_by(other.key):
                if other.name not in (record.name, None):
                    continue
                claim = context.claim(record.name)
                if claim.nonce != record.nonce or not self._in_force(record, claim, None, context):
                    continue
                self._set_holder(record.name, found.key)
                if found.name in (None, record.name):
                    found = replace(found, seen_holds=record.epoch)
        return found

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

    def _make_current(
        self, found: Binding, context: Context, also: Sequence[Binding] = ()
    ) -> Bound:
        """Write `found` as the name's holder: raise the hold count, record the boot, the
        holder and its nonce, and clear `lease_pid` (I1, I3, I4; KC5)."""
        rivals = self._rivals(found)
        done = self._db.execute(
            "UPDATE names SET epoch = epoch + 1, held_boot = ?, holder_harness = ?,"
            " holder_session = ?, nonce = ?, lease_pid = NULL WHERE name = ?",
            (found.boot, found.harness, found.session_id, found.nonce, found.name),
        )
        if done.rowcount != 1:
            raise RuntimeError(f"no name record for {found.name}")
        holds = self._name(found.name).epoch
        found = self._put(replace(found, seen_holds=holds))
        return Bound(found, self._supersede([*rivals, *also], found, context))

    def _rivals(self, found: Binding) -> list[Binding]:
        """The name's other bindings that hold it: current or provisional."""
        return [
            other
            for other in self._named(found.name)
            if other.key != found.key and other.state in _HOLDING
        ]

    def _supersede(
        self, rows: Sequence[Binding], by: Binding, context: Context
    ) -> tuple[Binding, ...]:
        done: dict[tuple[str, str], Binding] = {}
        for row in rows:
            if row.key == by.key or row.key in done or row.state == "superseded":
                continue
            generation = f" (generation {by.generation})" if by.generation is not None else ""
            reason = f"superseded by {by.harness} session {by.session_id}{generation}"
            done[row.key] = self._put(replace(row, state="superseded", reason=reason))
            self._keep_lease(done[row.key], context)
        return tuple(done.values())

    def _keep_lease(self, old: Binding, context: Context) -> None:
        """Step 5's holder rule (F4): a holder superseded by a session that is not its heir
        stays the holder and keeps the nonce; while a live claim carries the nonce, its pid
        becomes the name's `lease_pid`, which keeps the lease in force (KC5(b2)). A holder
        that points at another name sets nothing (X6)."""
        if old.pid is None:
            return
        for record in self._held_by(old.key):
            if old.name not in (record.name, None):
                continue
            claim = context.claim(record.name)
            if claim.state == "live" and claim.boot == old.boot and claim.nonce == record.nonce:
                self._db.execute(
                    "UPDATE names SET lease_pid = ? WHERE name = ?", (old.pid, record.name)
                )

    def _lapse(self, record: NameRecord, claim: lease.Claim, context: Context) -> NameRecord:
        """Clear `lease_pid` once the claim it kept has ended: replaced, removed or
        unreadable. The claim changes outside `bind`, so this runs at the next decision
        for the name."""
        if record.lease_pid is None:
            return record
        if claim.state == "live" and claim.boot == context.boot and claim.nonce == record.nonce:
            return record
        self._db.execute("UPDATE names SET lease_pid = NULL WHERE name = ?", (record.name,))
        return replace(record, lease_pid=None)

    def _same_process(self, caller: Caller, context: Context) -> list[Binding]:
        """Other sessions of the caller's own process: one process runs one session."""
        if caller.pid is None:
            return []
        found = self._db.execute(
            f"SELECT {_FIELDS} FROM {_ROWS} WHERE b.harness = ? AND b.pid = ? AND b.boot = ?"
            " AND b.session_id != ? AND b.state != 'superseded' ORDER BY b.bound_at, b.rowid",
            (caller.harness, caller.pid, context.boot, caller.session_id),
        )
        return [_binding(row) for row in found]

    def _holds_session(self, claim: lease.Claim, row: Binding) -> bool:
        """The live claim's session is this one, or another session of its process."""
        if claim.session == row.session_id:
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
            f"SELECT {_FIELDS} FROM {_ROWS} WHERE b.harness = ? AND b.session_id = ?",
            (harness, session_id),
        ).fetchone()
        return None if found is None else _binding(found)

    def _named(self, name: str) -> list[Binding]:
        found = self._db.execute(
            f"SELECT {_FIELDS} FROM {_ROWS} WHERE b.name = ? ORDER BY b.bound_at, b.rowid",
            (name,),
        )
        return [_binding(row) for row in found]

    def _put(self, row: Binding) -> Binding:
        """Write the row, and return it as the view reads it. A current row is seen bound now: its
        name's `bound_seen` moves to the row's `last_seen` (I5)."""
        if row.state == "current":
            self._db.execute(
                "UPDATE names SET bound_seen = ? WHERE name = ?", (row.last_seen, row.name)
            )
        self._db.execute(
            f"INSERT INTO bindings ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (harness, session_id) DO UPDATE SET"
            " name = excluded.name, pid = excluded.pid,"
            " evidence = excluded.evidence, boot = excluded.boot,"
            " generation = excluded.generation, state = excluded.state,"
            " reason = excluded.reason,"
            " bound_at = excluded.bound_at, last_seen = excluded.last_seen,"
            " seen_holds = excluded.seen_holds",
            (
                row.harness,
                row.session_id,
                row.name,
                row.pid,
                row.evidence,
                row.boot,
                row.generation,
                row.state,
                row.reason,
                row.bound_at,
                row.last_seen,
                row.seen_holds,
            ),
        )
        return self._get(row.harness, row.session_id)  # the view: nonce from the record

    def _record(self, name: str, context: Context) -> bool:
        """Make the name's record if it has none, rewrite its `declared` from the
        context, and return it (KC9). `first_bound_at` is set here, once."""
        declared = bool(context.declared(name))
        self._db.execute(
            "INSERT INTO names (name, declared, generation, epoch, first_bound_at)"
            " VALUES (?, ?, 0, 0, ?) ON CONFLICT (name) DO UPDATE SET declared = excluded.declared",
            (name, int(declared), context.now),
        )
        return declared

    def _name(self, name: str) -> NameRecord | None:
        found = self._db.execute(f"SELECT {_RECORD} FROM names WHERE name = ?", (name,))
        row = found.fetchone()
        return None if row is None else _name_record(row)

    def _held_by(self, key: tuple[str, str]) -> list[NameRecord]:
        """The records whose holder is this session."""
        found = self._db.execute(
            f"SELECT {_RECORD} FROM names WHERE holder_harness = ? AND holder_session = ?"
            " ORDER BY name",
            key,
        )
        return [_name_record(row) for row in found]

    def _holder(self, record: NameRecord) -> Binding | None:
        return None if record.holder is None else self._get(*record.holder)

    def _set_holder(self, name: str, key: tuple[str, str]) -> None:
        """A nameless successor takes the holder's place (KC4): no generation or epoch."""
        self._db.execute(
            "UPDATE names SET holder_harness = ?, holder_session = ?, lease_pid = NULL"
            " WHERE name = ?",
            (*key, name),
        )

    def _next_generation(self, name: str) -> int:
        """The name's next generation. Its record exists, since every decision under a
        name makes it first, so a missing one is a bug and raises (N2)."""
        done = self._db.execute(
            "UPDATE names SET generation = generation + 1 WHERE name = ?", (name,)
        )
        if done.rowcount != 1:
            raise RuntimeError(f"no name record for {name}")
        return self._name(name).generation


def _name_record(row: sqlite3.Row | tuple) -> NameRecord:
    holder = None if row[7] is None else (row[7], row[8])
    return NameRecord(row[0], bool(row[1]), *row[2:7], holder, row[9], row[10])


def _binding(row: sqlite3.Row | tuple) -> Binding:
    values = list(row)
    values[3] = bool(values[3])
    return Binding(*values)


def iso(seconds: float) -> str:
    """UTC, to the second, as the JSON records give times."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))
