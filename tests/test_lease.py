"""grove's claim, read only (KTD6; grove lease.mjs at 7d054e2)."""

import json
from datetime import UTC, datetime

import pytest

from mycelium import lease

BOOT = "boot-now"
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC).timestamp()


def iso(seconds_from_now: float) -> str:
    """A time as JavaScript's toISOString writes it, which is what grove writes."""
    moment = datetime.fromtimestamp(NOW + seconds_from_now, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def claim(**over) -> dict:
    base = {
        "state": "pending",
        "nonce": "n1",
        "resumer": "lead-01",
        "boot": BOOT,
        "expires": iso(600),
        "pane": None,
        "session": None,
    }
    return base | over


def write(tmp_path, data) -> object:
    path = lease.claim_path(tmp_path, "ogham-worker")
    path.parent.mkdir(parents=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data) + "\n")
    return path


def never(found):
    raise AssertionError("a pending claim needs no session check")


def test_the_claim_lives_where_grove_writes_it(tmp_path):
    path = lease.claim_path(tmp_path, "ogham-worker")
    assert path == tmp_path / ".kodhama" / "grove" / "local" / "ogham-worker" / "claim"


def test_no_claim_is_none_and_no_lease(tmp_path):
    found = lease.read(lease.claim_path(tmp_path, "ogham-worker"))
    assert found.state == "none"
    assert not lease.in_force(found, BOOT, NOW, never)


def test_a_pending_claim_not_yet_expired_is_in_force(tmp_path):
    found = lease.read(write(tmp_path, claim()))
    assert found.state == "pending"
    assert found.nonce == "n1"
    assert found.boot == BOOT
    assert found.expires == pytest.approx(NOW + 600)
    assert lease.in_force(found, BOOT, NOW, never)


def test_an_expired_pending_claim_is_not_in_force(tmp_path):
    found = lease.read(write(tmp_path, claim(expires=iso(-1))))
    assert found.state == "pending"
    assert not lease.in_force(found, BOOT, NOW, never)


def test_a_claim_from_an_earlier_boot_is_not_in_force(tmp_path):
    found = lease.read(write(tmp_path, claim(boot="boot-before")))
    assert not lease.in_force(found, BOOT, NOW, never)
    live = lease.read(write_live(tmp_path, boot="boot-before"))
    assert not lease.in_force(live, BOOT, NOW, lambda found: True)


def write_live(tmp_path, **over):
    path = lease.claim_path(tmp_path, "ogham-worker")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = claim(state="live", expires=None, pane="w1:p1", session="s-b") | over
    path.write_text(json.dumps(data))
    return path


def test_a_live_claim_whose_session_runs_is_in_force(tmp_path):
    found = lease.read(write_live(tmp_path))
    assert (found.state, found.session, found.pane) == ("live", "s-b", "w1:p1")
    asked = []

    def runs(seen):
        asked.append(seen)
        return True

    assert lease.in_force(found, BOOT, NOW, runs)
    assert asked == [found]


def test_a_live_claim_whose_session_is_dead_is_not_in_force(tmp_path):
    found = lease.read(write_live(tmp_path))
    assert not lease.in_force(found, BOOT, NOW, lambda seen: False)


@pytest.mark.parametrize(
    "data",
    [
        "{not json",
        json.dumps(["a list"]),
        json.dumps(claim(state="adopted")),
        json.dumps(claim(nonce="")),
        json.dumps(claim(boot=None)),
        json.dumps(claim(expires="tomorrow")),
        json.dumps(claim(expires="2026-11-10T12:10:00Z")),  # not as toISOString writes it
    ],
)
def test_a_claim_grove_did_not_write_in_full_is_unreadable_and_no_lease(tmp_path, data):
    found = lease.read(write(tmp_path, data))
    assert found.state == "unreadable"
    assert found.reason
    assert not lease.in_force(found, BOOT, NOW, never)


def test_a_claim_that_cannot_be_read_is_unreadable(tmp_path):
    path = lease.claim_path(tmp_path, "ogham-worker")
    path.mkdir(parents=True)  # a folder where the file should be
    assert lease.read(path).state == "unreadable"


def test_reading_a_claim_writes_nothing(tmp_path):
    path = write(tmp_path, claim())
    before = path.read_bytes()
    lease.read(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == ["claim"]
