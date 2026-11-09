"""Who is calling: the caller's harness, found by walking the process tree (KTD5).

KTD5 says the caller is never identified from an argument naming it, so
`resolve` takes no name. One `ps` call reads the whole process table, and the
walk runs in memory from the caller's parent up through parent links to the
nearest ancestor whose name (the basename of `comm`, matched case-sensitively,
so the desktop app `Claude` and `Claude Helper` are not harnesses) is a
harness: `claude` or `codex`, which U4 binds, or `droid` or `opencode`, which
resolve unbound (the 2026-10-29 ruling) so a Claude above them is never taken
as the caller. The rules come from hq `areas/agent-comms/research/probes-u2.md`
(S3, consequences 1 and 2):

- **Claude:** the harness pid is that `claude`. A nested Claude replaces
  `CLAUDE_PID` and `CLAUDE_CODE_SESSION_ID`, so the session id is taken only
  when `CLAUDE_PID` names that pid; an inherited value is never trusted.
- **Codex TUI:** a TUI runs its tools under the shared app-server daemon, a
  `codex` whose `comm` path holds `app-server-daemon` (probes S3 Codex). A codex
  parent alone does not mark it: a tool shell given one command replaces
  itself, so a nested `codex exec` can sit directly under the daemon. The daemon proves no session's
  liveness, so the pid is unknown, the session is `CODEX_THREAD_ID`, and
  nesting is undecidable; the daemon and the TUI that started it never count
  as nesting.
- **Codex exec:** the harness pid is that `codex`, the session is
  `CODEX_THREAD_ID`. A daemon above it is another session's harness and
  counts as nesting.
- **Nesting:** another harness-named process above the caller's harness pid.
- **No process table** (`ps` denied in Codex's sandbox, missing, or slow):
  `CODEX_THREAD_ID` identifies a Codex caller with pid and nesting unknown;
  otherwise the caller is unbound. Each harness inherits the other's
  variables, so `CODEX_THREAD_ID` beside a Claude marker (`CLAUDE_PID` or
  `CLAUDE_CODE_SESSION_ID`) means one runs inside the other, whichever way
  round: that caller is nested, so it is unbound (a marker inherited from
  wherever the shell started reads the same). Known limit: with no walk,
  a caller with no Claude marker reads as Codex with nesting unknown, so a
  Codex inside a Codex is not caught.

`source` says what the answer rests on: `walk` whenever the walk ran,
`codex-thread-id` when only the fallback identified the caller, and `none`
when neither did. macOS `comm` is how a process was exec'd, so a daemon
exec'd without its path reads as a codex exec with the shared pid (nested
while its starting TUI is still its parent). That Codex execs it by path is
unverified on a live Codex, and plan U11 confirms it. Linux `comm` is a short
name, so there the mark never matches. The walk guards against mislabelling,
not attack: a same-user process sets its own name
(docs/decisions/caller-identity.md).
"""

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

HARNESSES = ("claude", "codex")
"""Harnesses U4 binds."""

UNSUPPORTED = ("droid", "opencode")
"""Harnesses recognised in the walk but left unbound (U13, and a probe if Droid returns)."""

_KNOWN = HARNESSES + UNSUPPORTED

# By absolute path: through PATH, a caller could hide `ps` and force the thread-id fallback.
PS_COMMAND = ("/bin/ps", "-axww", "-o", "pid=,ppid=,comm=")

# A hook runs this inside its harness's hook timeout.
TIMEOUT_SECONDS = 2.0

DAEMON_MARK = "app-server-daemon"


class ProcessTableUnavailable(Exception):
    """`ps` could not be run, or gave no process table."""


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    comm: str

    @property
    def name(self) -> str:
        return self.comm.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Caller:
    harness: str | None
    """`claude` or `codex`; None when unbound."""
    pid: int | None
    """The harness process; None for a Codex TUI (shared daemon) or with no walk."""
    session_id: str | None
    nested: bool | None
    """None when undecidable."""
    source: str
    """`walk`, `codex-thread-id` or `none`."""
    reason: str | None
    """Why the caller is unbound, or what stays unknown; None when nothing does."""

    @property
    def resolved(self) -> bool:
        return self.harness is not None


def parse_ps(text: str) -> dict[int, Proc]:
    """`pid ppid comm` lines into a table; `comm` may hold spaces. Malformed lines are skipped."""
    table: dict[int, Proc] = {}
    for line in text.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        table[pid] = Proc(pid, ppid, fields[2].strip())
    return table


def process_table(
    argv: Sequence[str] = PS_COMMAND, timeout: float = TIMEOUT_SECONDS
) -> dict[int, Proc]:
    """The whole process table, in one `ps` call. Raises ProcessTableUnavailable."""
    name = argv[0]
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise ProcessTableUnavailable(f"{name} not found") from None
    except subprocess.TimeoutExpired:
        raise ProcessTableUnavailable(f"{name} did not answer within {timeout:g}s") from None
    except OSError as error:
        raise ProcessTableUnavailable(f"{name} can't be run: {error}") from None
    if done.returncode != 0:
        said = done.stderr.strip().splitlines()
        detail = f": {said[-1]}" if said else ""
        raise ProcessTableUnavailable(f"{name} exited {done.returncode}{detail}")
    table = parse_ps(done.stdout)
    if not table:
        raise ProcessTableUnavailable(f"{name} listed no processes")
    return table


def resolve(
    env: Mapping[str, str] | None = None,
    pid: int | None = None,
    table: Callable[[], Mapping[int, Proc]] = process_table,
) -> Caller:
    """The caller of process `pid` (this process by default), from the process walk.

    `env` must be that same process's environment (this one's by default): the
    Claude session and Codex thread checks read it. Test `resolved`, not `reason`:
    a resolved caller can carry a reason naming what stays unknown.
    """
    env = os.environ if env is None else env
    pid = os.getpid() if pid is None else pid
    thread = _value(env, "CODEX_THREAD_ID")
    try:
        procs = table()
    except ProcessTableUnavailable as error:
        why = f"the process walk could not run ({error})"
        if thread is not None and _claude_marker(env):
            reason = (
                f"{why}; CODEX_THREAD_ID sits beside a Claude marker, so one harness runs "
                "inside the other and the caller is nested"
            )
            return Caller(None, None, None, True, "none", reason)
        if thread is not None:
            reason = f"{why}; CODEX_THREAD_ID names the session, so pid and nesting are unknown"
            return Caller("codex", None, thread, None, "codex-thread-id", reason)
        return _unbound(f"{why}, and CODEX_THREAD_ID is not set", source="none")

    if pid not in procs:
        return _unbound(f"no harness ancestor: pid {pid} is not in the process table")
    ancestors = _ancestors(procs, pid)
    nearest = next((i for i, p in enumerate(ancestors) if p.name in _KNOWN), None)
    if nearest is None:
        return _unbound("no harness ancestor")
    harness = ancestors[nearest]
    nested = any(p.name in _KNOWN for p in ancestors[nearest + 1 :])

    if harness.name in UNSUPPORTED:
        return _unbound(
            f"the nearest harness is {harness.name} (pid {harness.pid}); "
            "U4 binds Claude and Codex only"
        )
    if harness.name == "claude":
        own = _value(env, "CLAUDE_PID") == str(harness.pid)
        session = _value(env, "CLAUDE_CODE_SESSION_ID") if own else None
        if not own:
            reason = f"CLAUDE_PID does not name claude pid {harness.pid}, so the session is unknown"
        elif session is None:
            reason = "CLAUDE_CODE_SESSION_ID is not set, so the session is unknown"
        else:
            reason = None
        return Caller("claude", harness.pid, session, nested, "walk", reason)
    if DAEMON_MARK in harness.comm:
        if thread is None:
            return _unbound(
                "a Codex TUI caller (under the shared app-server daemon) without CODEX_THREAD_ID"
            )
        reason = "a Codex TUI caller: the daemon is shared, so pid and nesting are unknown"
        return Caller("codex", None, thread, None, "walk", reason)
    reason = None if thread is not None else "CODEX_THREAD_ID is not set, so the session is unknown"
    return Caller("codex", harness.pid, thread, nested, "walk", reason)


def _ancestors(procs: Mapping[int, Proc], pid: int) -> list[Proc]:
    """The parent of `pid`, its parent, and so on; stops at a missing pid or a cycle."""
    found: list[Proc] = []
    seen = {pid}
    current = procs[pid].ppid
    while current not in seen and current in procs:
        seen.add(current)
        found.append(procs[current])
        current = procs[current].ppid
    return found


def _unbound(reason: str, source: str = "walk") -> Caller:
    return Caller(None, None, None, None, source, reason)


def _claude_marker(env: Mapping[str, str]) -> bool:
    return any(_value(env, key) for key in ("CLAUDE_PID", "CLAUDE_CODE_SESSION_ID"))


def _value(env: Mapping[str, str], key: str) -> str | None:
    return env.get(key) or None
