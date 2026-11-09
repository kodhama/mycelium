"""The one boundary to herdr, read-only (KTD19; docs/decisions/caller-identity.md).

Every herdr call mycelium makes goes through this module, and here it only
reads: find the pane a process runs in, read the pane's identity, and check
its foreground processes (T9). Typing into a pane is U12's, not this module's.

herdr 0.9.3, probed live on 2026-11-09:

- Every reply is JSON `{"id": ..., "result": {...}}` on stdout, exit 0.
- `pane get <id>` answers `{"pane": {...}, "type": "pane_info"}`. The record
  holds `pane_id`, `agent`, `agent_session` (`{agent, kind, source, value}`,
  the harness's session id in `value`), `terminal_title_stripped`, `cwd`,
  `terminal_id` and more, but **no name**. A pane with no agent omits
  `agent` and `agent_session`.
- `agent get <id>` answers `{"agent": {...}, "type": "agent_info"}`: the same
  fields plus `name`, the live agent name. It may not exist yet at
  SessionStart (lessons C2), and a pane with no agent has none.
- `pane process-info --pane <id>` answers `{"process_info": {...}}` with
  `shell_pid` and `foreground_processes`, each with `pid`, `name`, `argv`,
  `cwd`. A listed pane herdr could not restore (it carries `restore_error`)
  answers it as not found.
- `pane list` answers `{"panes": [<pane record>...]}`.
- Not found is a JSON error on stderr, nothing on stdout, and exit 1, such as
  `{"error": {"code": "pane_not_found", "message": "pane w0:p0 not found"}, "id": ...}`;
  `agent get` says `agent_not_found` for both an unknown pane and a pane with
  no agent record.

The module exposes the pane's title and the agent record's name side by side
and chooses neither: which one is the launch name is U4b's rule. The name is
kept only when the agent record's session is the pane's, since the two are
read by separate calls. The terminal id is reported, never used as a key
(lessons C1). Every other herdr failure raises `PaneUnavailable`, as
`clusters.HelperUnavailable` does for its helper.
"""

import json
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

COMMAND = ("herdr",)

# A hook may run a pane read inside its harness's hook timeout.
TIMEOUT_SECONDS = 5.0


class PaneUnavailable(Exception):
    """herdr could not be run, or its answer was not the expected shape."""


class _Stalled(PaneUnavailable):
    """herdr did not answer in time; a scan stops rather than wait on every pane."""


@dataclass(frozen=True)
class ForegroundProcess:
    pid: int
    name: str
    argv: tuple[str, ...]
    cwd: str | None


@dataclass(frozen=True)
class PaneIdentity:
    pane_id: str
    agent: str | None
    """The harness herdr detected in the pane, such as `claude`; `None` for a plain shell."""
    agent_session_id: str | None
    """The harness's session id, from the pane record's `agent_session.value`."""
    title: str | None
    """The terminal title with herdr's status marks stripped (`terminal_title_stripped`)."""
    cwd: str | None
    terminal_id: str | None
    """Reported for display; never a key (lessons C1)."""
    agent_name: str | None
    """The live name from `agent get`; `None` when herdr holds no agent record (lessons C2)
    or the record's session is not the pane's."""


def read_identity(
    pane_id: str, command: Sequence[str] = COMMAND, timeout: float = TIMEOUT_SECONDS
) -> PaneIdentity | None:
    """The pane's identity, or None when herdr knows no such pane. Raises PaneUnavailable."""
    result = _ask(command, ("pane", "get", pane_id), timeout, missing="pane_not_found")
    if result is None:
        return None
    agent = _ask(command, ("agent", "get", pane_id), timeout, missing="agent_not_found")
    try:
        record = _object(result.get("pane"), "pane")
        session_id = _session(record)
        agent_name = None
        if agent is not None:
            agent_record = _object(agent.get("agent"), "agent")
            # Two calls: a pane replaced between them, or a record stale after /clear,
            # must not pair this session's id with another session's name.
            if _session(agent_record) == session_id:
                agent_name = _optional_text(agent_record.get("name"), "name")
        return PaneIdentity(
            pane_id=_text(record.get("pane_id"), "pane_id"),
            agent=_optional_text(record.get("agent"), "agent"),
            agent_session_id=session_id,
            title=_optional_text(record.get("terminal_title_stripped"), "title"),
            cwd=_optional_text(record.get("cwd"), "cwd"),
            terminal_id=_optional_text(record.get("terminal_id"), "terminal_id"),
            agent_name=agent_name,
        )
    except (ValueError, TypeError) as error:
        raise _unexpected(error) from None


def foreground(
    pane_id: str, command: Sequence[str] = COMMAND, timeout: float = TIMEOUT_SECONDS
) -> list[ForegroundProcess]:
    """The pane's foreground processes; empty when herdr knows no such live pane."""
    args = ("pane", "process-info", "--pane", pane_id)
    result = _ask(command, args, timeout, missing="pane_not_found")
    if result is None:
        return []
    try:
        info = _object(result.get("process_info"), "process_info")
        processes = info.get("foreground_processes")
        if not isinstance(processes, list):
            raise ValueError("foreground_processes is not a list")
        return [_process(item) for item in processes]
    except (ValueError, TypeError) as error:
        raise _unexpected(error) from None


def foreground_is(
    pane_id: str,
    pid: int,
    *,
    name: str | None = None,
    cwd: str | None = None,
    command: Sequence[str] = COMMAND,
    timeout: float = TIMEOUT_SECONDS,
) -> bool:
    """T9: the pid is among the pane's foreground processes, with this name and cwd when given."""
    return any(
        process.pid == pid
        and (name is None or process.name == name)
        and (cwd is None or process.cwd == cwd)
        for process in foreground(pane_id, command=command, timeout=timeout)
    )


def find_pane(
    pid: int,
    hint: str | None = None,
    command: Sequence[str] = COMMAND,
    timeout: float = TIMEOUT_SECONDS,
) -> str | None:
    """The pane whose foreground holds the pid, or None when no pane does.

    The hint (an `HERDR_PANE_ID` value) is tried first and accepted only when
    its foreground holds the pid: a child inherits its parent's value, so the
    value alone proves nothing (lessons C2). Otherwise every listed pane is
    checked. A pane herdr can't read is skipped, and if no other pane holds the
    pid, the scan raises PaneUnavailable rather than answer None on a guess.
    The whole lookup shares one `timeout`, since a hook runs it inside its
    harness's own; running out raises PaneUnavailable.
    """
    deadline = time.monotonic() + timeout
    unread: list[str] = []

    def holds(pane_id: str) -> bool:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _Stalled(f"herdr did not answer within {timeout:g}s")
        try:
            return foreground_is(pane_id, pid, command=command, timeout=left)
        except _Stalled:
            raise  # a stalled herdr stalls every pane: stop here
        except PaneUnavailable as error:  # one pane herdr can't read must not hide the others
            unread.append(f"{pane_id} ({error})")
            return False

    if hint and holds(hint):
        return hint
    for pane_id in _pane_ids(command, max(deadline - time.monotonic(), 0.001)):
        if pane_id != hint and holds(pane_id):
            return pane_id
    if unread:
        raise PaneUnavailable(f"no readable pane holds pid {pid}; unreadable: {'; '.join(unread)}")
    return None


def _session(record: dict[str, Any]) -> str | None:
    """A pane or agent record's `agent_session.value`; None when it has none."""
    session = record.get("agent_session")
    if session is None:
        return None
    return _optional_text(_object(session, "agent_session").get("value"), "session")


def _pane_ids(command: Sequence[str], timeout: float) -> list[str]:
    result = _ask(command, ("pane", "list"), timeout, missing=None)
    try:
        panes = result.get("panes")
        if not isinstance(panes, list):
            raise ValueError("panes is not a list")
        return [_text(_object(item, "pane").get("pane_id"), "pane_id") for item in panes]
    except (ValueError, TypeError) as error:
        raise _unexpected(error) from None


def _ask(
    command: Sequence[str], args: Sequence[str], timeout: float, missing: str | None
) -> dict[str, Any] | None:
    """One herdr call's `result`, or None when herdr answers with the `missing` error code."""
    name = command[0]
    try:
        done = subprocess.run(
            [*command, *args],
            capture_output=True,
            encoding="utf-8",  # herdr writes UTF-8 (titles carry glyphs), whatever the locale
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise PaneUnavailable(f"herdr ({name}) is not installed") from None
    except subprocess.TimeoutExpired:
        raise _Stalled(f"herdr did not answer within {timeout:g}s") from None
    except OSError as error:  # not executable, or not a program this system can run
        raise PaneUnavailable(f"herdr ({name}) can't be run: {error}") from None
    if done.returncode != 0:
        # herdr 0.9.3 writes its JSON error to stderr; stdout is read too in case that moves.
        code, message = _error(done.stderr)
        if code is None:
            code, message = _error(done.stdout)
        if missing is not None and code == missing:
            return None
        said = message or _last_line(done.stderr) or _last_line(done.stdout)
        detail = f": {said}" if said else ""
        raise PaneUnavailable(f"herdr exited {done.returncode}{detail}")
    try:
        answer = json.loads(done.stdout)
        if not isinstance(answer, dict):
            raise ValueError("the reply is not an object")
        return _object(answer.get("result"), "result")
    except ValueError as error:
        raise _unexpected(error) from None


def _error(text: str) -> tuple[str | None, str | None]:
    """The code and message of herdr's JSON error reply, or (None, None)."""
    try:
        error = json.loads(text).get("error")
        code, message = error.get("code"), error.get("message")
    except (ValueError, AttributeError):
        return None, None
    return (
        code if isinstance(code, str) else None,
        message if isinstance(message, str) else None,
    )


def _process(item: Any) -> ForegroundProcess:
    item = _object(item, "foreground process")
    pid = item.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        raise ValueError("a foreground pid is not an integer")
    argv = item.get("argv")
    if not isinstance(argv, list) or not all(isinstance(part, str) for part in argv):
        raise ValueError("a foreground argv is not a list of strings")
    return ForegroundProcess(
        pid=pid,
        name=_text(item.get("name"), "name"),
        argv=tuple(argv),
        cwd=_optional_text(item.get("cwd"), "cwd"),
    )


def _object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} is not an object")
    return value


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} is not a non-empty string")
    return value


def _optional_text(value: Any, what: str) -> str | None:
    """A string, or None when absent or empty: an empty name is no name."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{what} is not a string")
    return value


def _last_line(text: str) -> str | None:
    lines = text.strip().splitlines()
    return lines[-1] if lines else None


def _unexpected(error: Exception) -> PaneUnavailable:
    return PaneUnavailable(f"herdr gave unexpected output: {error}")
