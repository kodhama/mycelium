"""The directory: each name's deliverability, harness, last seen and knock (R5, R36, R37; KTD3)."""

import shutil
import sqlite3

import pytest
from test_bindings import NOW, World, claude, codex_tui

from mycelium import directory
from mycelium.index import HOLD_SECONDS, Hold, Index, refresh
from mycelium.launch import Launch
from mycelium.store import SCHEMA_VERSION, StoreRefused, open_store
from mycelium.topology import Slice

DAY = 24 * 60 * 60


def declared(name: str, run: str) -> Slice:
    return Slice(
        name=name,
        declared_name=name,
        home="/home/maintainer/hq",
        role=None,
        run=run,
        mission="",
        manager="@maintainer",
        known=True,
        orphan=False,
    )


@pytest.fixture
def world(tmp_path):
    found = World(tmp_path)
    found.declared = {"w-running", "w-parked", "w-done"}
    yield found
    found.store.close()


INDEX = Index(
    {
        "w-running": declared("w-running", "running"),
        "w-parked": declared("w-parked", "parked"),
        "w-done": declared("w-done", "done"),
    },
    {},
    {"w-held": Hold("w-held", NOW - DAY, "w-running")},
)


def nothing_unhandled(name: str) -> bool:
    return False


def table(world, index=INDEX, unhandled=nothing_unhandled):
    found = directory.rows(index, world.store.rows(), world.context(), unhandled)
    return {row.name: row for row in found}


def test_a_running_name_with_a_live_binding_is_live_and_knockable(world):
    world.bind(claude(1001, "s-1"), launch="w-running")
    row = table(world)["w-running"]
    assert row.deliverability == "live"
    assert row.declared is True
    assert row.harness == "claude"
    assert row.generation == 1
    assert row.knock == {"can": True, "how": "sendmessage-hint"}
    assert row.last_seen == "2027-01-15T08:00:01Z"


def test_a_running_name_with_no_live_binding_is_absent(world):
    rows = table(world)
    assert rows["w-running"].deliverability == "absent"
    assert rows["w-running"].knock == {"can": False, "how": None}
    assert rows["w-running"].harness is None
    assert rows["w-running"].last_seen is None


def test_after_a_reboot_a_running_name_is_absent_never_gone(world):
    world.bind(claude(1001, "s-1"), launch="w-running")
    world.boot = "boot-after"
    row = table(world)["w-running"]
    assert row.deliverability == "absent"
    assert row.knock["can"] is False
    assert row.harness == "claude"  # the dead binding still says who it was


def test_a_parked_name_is_queue_only_even_when_bound(world):
    world.bind(claude(1001, "s-1"), launch="w-parked")
    row = table(world)["w-parked"]
    assert row.deliverability == "queue-only"
    assert row.knock == {"can": False, "how": None}


def test_a_done_name_is_gone(world):
    assert table(world)["w-done"].deliverability == "gone"


def test_a_held_name_is_held_until_its_hold_ends(world):
    row = table(world)["w-held"]
    assert (row.deliverability, row.declared) == ("held", False)
    world.now = NOW - DAY + HOLD_SECONDS
    assert table(world)["w-held"].deliverability == "gone"


def test_a_codex_name_is_knocked_by_its_queue(world):
    world.bind(codex_tui("t-1"), launch="w-running")
    assert table(world)["w-running"].knock == {"can": True, "how": "codex-queue"}


def test_a_provisional_binding_does_not_make_a_name_live(world):
    world.claim(name="w-running", nonce="n1")
    world.bind(claude(1001, "s-1"), launch="w-running")
    assert table(world)["w-running"].deliverability == "absent"


# R37: an undeclared name.


def test_an_undeclared_name_is_live_while_bound(world):
    world.bind(claude(1001, "s-1"), launch="scratch-probe")
    row = table(world)["scratch-probe"]
    assert (row.declared, row.deliverability) == (False, "live")
    assert row.knock["can"] is True


def test_an_undeclared_name_is_queue_only_when_unbound(world):
    world.bind(claude(1001, "s-1"), launch="scratch-probe")
    world.alive.clear()
    world.now += 7 * DAY - 2
    assert table(world)["scratch-probe"].deliverability == "queue-only"


def test_an_undeclared_name_is_gone_after_seven_days_unbound_with_nothing_unhandled(world):
    world.bind(claude(1001, "s-1"), launch="scratch-probe")
    world.alive.clear()
    world.now += 7 * DAY
    assert table(world)["scratch-probe"].deliverability == "gone"


def test_an_undeclared_name_touched_within_seven_days_is_not_gone(world):
    probe = claude(1001, "s-1")
    world.bind(probe, launch="scratch-probe")
    world.now += 6 * DAY
    world.store.touch(probe, world.context())  # a prompt, six days in
    world.alive.clear()
    world.now += 2 * DAY
    assert table(world)["scratch-probe"].deliverability == "queue-only"


def test_an_undeclared_name_with_unhandled_mail_is_kept(world):
    world.bind(claude(1001, "s-1"), launch="scratch-probe")
    world.alive.clear()
    world.now += 30 * DAY
    rows = table(world, unhandled=lambda name: name == "scratch-probe")
    assert rows["scratch-probe"].deliverability == "queue-only"


def test_every_declared_held_and_bound_name_is_listed_once(world):
    world.bind(claude(1001, "s-1"), launch="scratch-probe")
    world.bind(claude(1002, "s-2"), launch="w-running")
    world.bind(claude(1003, "s-3"), launch="w-running")  # the name, twice: listed once
    world.bind(claude(1004, "s-4"), launch=Launch(None, "no launch name", {}, "none"))
    names = [row.name for row in directory.rows(INDEX, world.store.rows(), world.context())]
    assert names == sorted(["w-running", "w-parked", "w-done", "w-held", "scratch-probe"])


def test_the_declared_names_are_read_once_per_directory(world, monkeypatch):
    for pid, name in ((1001, "w-running"), (1002, "w-parked"), (1003, "scratch-probe")):
        world.bind(claude(pid, f"s-{pid}"), launch=name)
    calls = []
    names = Index.names
    monkeypatch.setattr(Index, "names", lambda self: calls.append(1) or names(self))
    directory.rows(INDEX, world.store.rows(), world.context())
    assert len(calls) == 1


def test_a_row_reads_as_json_in_the_documented_shape(world):
    world.bind(claude(1001, "s-1"), launch="w-running")
    row = table(world)["w-running"]
    assert row.to_json() == {
        "name": "w-running",
        "declared": True,
        "deliverability": "live",
        "harness": "claude",
        "generation": 1,
        "state": "current",
        "last_seen": "2027-01-15T08:00:01Z",
        "knock": {"can": True, "how": "sendmessage-hint"},
    }
    assert set(directory.DELIVERABILITY) == {"live", "absent", "queue-only", "held", "gone"}


# Against the real index and the real message store.


def test_the_directory_reads_the_index_as_refresh_builds_it(world, tree):
    found = tree("org")
    first = refresh(found.paths, read=found.read, now=1000.0)
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    index = refresh(found.paths, read=found.read, now=2000.0).index
    world.now = 2000.0
    rows = {row.name: row for row in directory.rows(index, [], world.context())}
    assert set(rows) == set(first.index.names())
    assert rows["mq-pm"].deliverability == "absent"
    assert rows["mq-roadmap-assistant"].deliverability == "queue-only"
    assert rows["ex-engine-lead-427"].deliverability == "held"


def unhandled(path, name: str) -> bool:
    with directory.unhandled_in(path) as found:
        return found(name)


def test_unhandled_mail_is_read_from_the_message_store(tmp_path):
    path = tmp_path / "data" / "store.sqlite3"
    assert unhandled(path, "scratch-probe") is False
    assert not path.exists()  # a read creates nothing (KTD13)
    store = open_store(path)
    message = store.append("scratch-probe", "hello")
    store.close()
    assert unhandled(path, "scratch-probe") is True
    store = open_store(path)
    store.mark_handled("scratch-probe", message)
    store.close()
    assert unhandled(path, "scratch-probe") is False


def test_a_symlinked_message_store_is_refused_never_followed(tmp_path):
    real = tmp_path / "elsewhere" / "store.sqlite3"
    store = open_store(real)
    store.append("scratch-probe", "hello")
    store.close()
    path = tmp_path / "data" / "store.sqlite3"
    path.parent.mkdir(mode=0o700)
    path.symlink_to(real)
    with pytest.raises(StoreRefused, match="symlink"):
        unhandled(path, "scratch-probe")


def test_a_message_store_from_a_newer_mycelium_is_refused(tmp_path):
    path = tmp_path / "data" / "store.sqlite3"
    open_store(path).close()
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(StoreRefused, match="newer"):
        unhandled(path, "scratch-probe")


def test_the_message_store_is_opened_once_per_directory(world, tmp_path, monkeypatch):
    for session, name in (("s-1", "probe-a"), ("s-2", "probe-b")):
        world.bind(claude(1001, session), launch=name)
    world.alive.clear()
    world.now += 8 * DAY
    path = tmp_path / "data" / "store.sqlite3"
    open_store(path).close()
    opened = []
    monkeypatch.setattr(directory, "open_store", lambda p: opened.append(p) or open_store(p))
    with directory.unhandled_in(path) as found:
        rows = {
            row.name: row
            for row in directory.rows(INDEX, world.store.rows(), world.context(), found)
        }
    assert rows["probe-a"].deliverability == rows["probe-b"].deliverability == "gone"
    assert opened == [path]


# The lead's ruling (i) (GRO-50 lead note, 2026-11-10): a SessionStart with no launch
# name keeps the row's old name, and the directory shows that row as unbound.

NO_NAME = Launch(None, "no launch name", {}, "none")
RENAMED = Launch(None, "launch names disagree", {"pane": "w-parked"}, "named")


@pytest.mark.parametrize("name", ["w-running", "scratch-probe"])
def test_a_no_name_row_shows_as_unbound_and_never_makes_its_name_live(world, name):
    probe = claude(1001, "s-1")
    world.bind(probe, launch=name)
    world.bind(probe, launch=RENAMED, source="resume")  # G2 unbinds it
    kept = world.bind(probe, launch=NO_NAME, source="resume").binding
    assert (kept.name, kept.state, kept.generation) == (name, "unbound", 1)
    row = table(world)[name]
    assert row.state == "unbound"
    assert row.deliverability == ("absent" if name == "w-running" else "queue-only")
    assert row.knock == {"can": False, "how": None}


def test_a_name_with_no_binding_shows_no_state(world):
    assert table(world)["w-running"].state is None


def test_the_message_store_is_looked_for_once_per_directory(tmp_path):
    path = tmp_path / "data" / "store.sqlite3"
    with directory.unhandled_in(path) as found:
        assert found("probe-a") is False
        store = open_store(path)  # the store appears in the middle of the listing
        store.append("probe-b", "hello")
        store.close()
        assert found("probe-b") is False  # the listing decided at its first question
