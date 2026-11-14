"""grove's lease claim, read only (KTD6; cluster spec §1 and §5.4).

grove writes one JSON file per agent at `<home>/.kodhama/grove/local/<name>/claim`
(grove `skills/supervision-setup/scripts/lease.mjs` at 7d054e2):

    {"state": "pending|live", "nonce", "resumer", "boot", "expires", "pane", "session"}

- `expires` is a timestamp as JavaScript's `toISOString` writes it
  (`2026-11-10T12:10:00.000Z`) on a `pending` claim, and null once `live`.
- `pane` and `session` are null until a session adopts the claim, which
  records its pane and harness session id.

A claim is judged the way grove's `readClaim` judges one: a claim whose state,
nonce or boot id is missing, or a pending one whose expiry is not a timestamp
grove writes, is `unreadable`. mycelium never writes the claim; grove's
`adopt` moves it from pending to live.

"A lease exists" (KTD6) is `in_force`: a claim from this boot that is pending
and unexpired, or live with a session the caller confirms is running. mycelium
confirms that from its own bindings (docs/decisions/bindings.md).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# toISOString: always UTC, always milliseconds.
_LEASE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


@dataclass(frozen=True)
class Claim:
    state: str
    """`pending`, `live`, `none` (no claim) or `unreadable`."""
    nonce: str | None = None
    resumer: str | None = None
    boot: str | None = None
    expires: float | None = None
    """Seconds since the epoch; None once live."""
    pane: str | None = None
    session: str | None = None
    reason: str | None = None
    """Why an `unreadable` claim can't be judged."""


def claim_path(home: str | Path, name: str) -> Path:
    """`<home>/.kodhama/grove/local/<name>/claim` (cluster spec §1)."""
    return Path(home) / ".kodhama" / "grove" / "local" / name / "claim"


def read(path: Path) -> Claim:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Claim("none")
    except (OSError, UnicodeDecodeError) as error:
        return _unreadable(f"cannot read the claim: {error}")
    try:
        data = json.loads(text)
    except ValueError:
        return _unreadable("the claim is not JSON")
    if not isinstance(data, dict) or data.get("state") not in ("pending", "live"):
        return _unreadable("the claim has no state or nonce")
    nonce = data.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return _unreadable("the claim has no state or nonce")
    boot = data.get("boot")
    if not isinstance(boot, str) or not boot:
        return _unreadable("the claim has no boot id")
    expires = _lease_time(data.get("expires"))
    if data["state"] == "pending" and expires is None:
        return _unreadable("the pending claim's expiry is not a timestamp the lease writes")
    return Claim(
        state=data["state"],
        nonce=nonce,
        resumer=_text(data.get("resumer")),
        boot=boot,
        expires=expires,
        pane=_text(data.get("pane")),
        session=_text(data.get("session")),
    )


def in_force(claim: Claim, boot: str | None, now: float, runs: Callable[[Claim], bool]) -> bool:
    """Whether a lease exists (KTD6). `runs` says whether a live claim's session runs."""
    if claim.state not in ("pending", "live") or boot is None or claim.boot != boot:
        return False
    if claim.state == "pending":
        return claim.expires is not None and claim.expires > now
    return runs(claim)


def _lease_time(value: object) -> float | None:
    if not isinstance(value, str) or not _LEASE_TIME.fullmatch(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _unreadable(reason: str) -> Claim:
    return Claim("unreadable", reason=reason)
