"""The launch name: which role a session was started as (R2, R3; hub plan U4).

Three sources can name it:

- **harness:** the harness's own session name, which the hook's adapter
  passes in (Claude's `-n`, for one);
- **environment:** `KODHAMA_AGENT`, which grove's start-session sets;
- **herdr:** the live agent name herdr holds for the session's pane, read
  through `mycelium.pane`. It counts only when the pane's foreground holds
  the walk's harness pid (lessons T9) and the pane's agent session is the
  walk's session: an inherited `HERDR_PANE_ID` alone, or a record left from
  another session, proves nothing (lessons C2). The pane title never counts,
  since it can be auto-generated.

Every source present must agree (R3): none gives no name, and a disagreement
gives no name either. A herdr that can't be read is unknown, never a match,
and leaves the other sources to decide. One deadline covers the pane scan and
both identity reads, since a hook runs inside its harness's timeout.
`@maintainer` is never a launch name (R35).
"""

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from mycelium import pane
from mycelium.identity import Caller
from mycelium.topology import MAINTAINER

ENVIRONMENT = "KODHAMA_AGENT"
TIMEOUT_SECONDS = pane.TIMEOUT_SECONDS


@dataclass(frozen=True)
class Launch:
    name: str | None
    """The launch name; None when no source gives one or the sources disagree."""
    reason: str | None
    """Why there is no name; None when there is one."""
    names: Mapping[str, str] = field(default_factory=dict)
    """Each source that gave a name: `harness`, `environment` or `pane` (herdr's name)."""
    herdr: str = "unchecked"
    """`named`, `none` (no pane holds the pid, or herdr holds no name), `stale` (the
    pane's record is another session's), `unknown` (herdr could not be read) or
    `unchecked` (no pid or session to check it against)."""

    def disagrees_with(self, name: str) -> bool:
        """Whether any source now names someone other than `name` (lessons G2)."""
        return any(found != name for found in self.names.values())


def resolve(
    caller: Caller,
    harness_name: str | None,
    env: Mapping[str, str] | None = None,
    *,
    command: Sequence[str] = pane.COMMAND,
    timeout: float = TIMEOUT_SECONDS,
) -> Launch:
    env = os.environ if env is None else env
    names: dict[str, str] = {}
    if harness_name:
        names["harness"] = harness_name
    if env.get(ENVIRONMENT):
        names["environment"] = env[ENVIRONMENT]
    herdr, herdr_name = _herdr(caller, env, command, timeout)
    if herdr_name is not None:
        names["pane"] = herdr_name
    if MAINTAINER in names.values():
        reason = f"{MAINTAINER} is reserved and is never bound to a session (R35)"
        return Launch(None, reason, names, herdr)
    if len(set(names.values())) > 1:
        said = ", ".join(f"{source} {name}" for source, name in names.items())
        return Launch(None, f"launch names disagree: {said} (R3)", names, herdr)
    if not names:
        reason = f"no launch name: no harness name, no {ENVIRONMENT}, and {_HERDR_SAYS[herdr]}"
        return Launch(None, reason, names, herdr)
    return Launch(next(iter(names.values())), None, names, herdr)


_HERDR_SAYS = {
    "none": "herdr holds no name for the session's pane",
    "stale": "herdr's record for the pane is another session's",
    "unknown": "herdr could not be read",
    "unchecked": "herdr can't be checked without the harness pid and session",
}


def _herdr(
    caller: Caller, env: Mapping[str, str], command: Sequence[str], timeout: float
) -> tuple[str, str | None]:
    if caller.pid is None or caller.session_id is None:
        return "unchecked", None
    deadline = time.monotonic() + timeout
    try:
        found = pane.find_pane(
            caller.pid, env.get("HERDR_PANE_ID") or None, command=command, timeout=timeout
        )
        if found is None:
            return "none", None
        left = deadline - time.monotonic()
        if left <= 0:
            return "unknown", None
        identity = pane.read_identity(found, command=command, timeout=left)
    except pane.PaneUnavailable:
        return "unknown", None
    if identity is None:
        return "none", None
    if identity.agent_session_id != caller.session_id:
        return "stale", None
    if identity.agent_name is None:
        return "none", None
    return "named", identity.agent_name
