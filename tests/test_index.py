"""The index's revision check, cache, last-good entries and holds (hub plan U14)."""

import fcntl
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium import index as index_module
from mycelium.clusters import HelperUnavailable
from mycelium.index import HAND_EDITED, HOLD_SECONDS, Change, IndexUnavailable, refresh

FAKE = Path(__file__).parent / "fake_cluster_helper.py"


def slices(index):
    return {name: index.slice(name) for name in index.names()}


def test_unchanged_revisions_read_the_cache_without_rebuilding(tree):
    found = tree("org")
    first = refresh(found.paths, read=found.read)
    second = refresh(found.paths, read=found.read)
    assert first.rebuilt
    assert not second.rebuilt
    assert slices(second.index) == slices(first.index)


def test_wiping_the_cache_rebuilds_the_same_index_and_changes_nothing(tree):
    found = tree("org")
    first = refresh(found.paths, read=found.read)
    shutil.rmtree(found.paths.cache)
    again = refresh(found.paths, read=found.read)
    assert again.rebuilt
    assert again.changes == ()
    assert slices(again.index) == slices(first.index)


def test_the_first_build_reports_every_name_as_added(tree):
    found = tree("internal-reports")
    changes = refresh(found.paths, read=found.read).changes
    assert {change.name for change in changes if change.kind == "added"} == {
        "root-lead",
        "t-dev",
        "t-docs",
        "t-lead",
        "t-qa",
    }


def test_a_park_written_between_two_calls_is_seen_by_the_second(tree):
    found = tree("org")
    assert refresh(found.paths, read=found.read).index.run("ex-engine-lead-427") == "running"
    found.edit("hq/mq/repos/engine", 'run = "running"', 'run = "parked"')
    later = refresh(found.paths, read=found.read)
    assert later.rebuilt
    assert later.index.run("ex-engine-lead-427") == "parked"
    assert later.index.slice("ex-engine-lead-427").stale is None


def test_a_manager_edit_is_reported_as_a_change(tree):
    found = tree("internal-reports")
    refresh(found.paths, read=found.read)
    found.edit("hq/team", 'manager = "t-dev"', 'manager = "t-lead"')
    later = refresh(found.paths, read=found.read)
    assert later.changes == (Change("manager", "t-qa", old="t-dev", new="t-lead"),)
    assert later.index.manager("t-qa") == "t-lead"


def test_a_hand_edit_keeps_the_last_good_entry_marked_stale(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "changed by hand"', seal=False)

    index = refresh(found.paths, read=found.read).index

    assert HAND_EDITED in index.errors()[found.home("hq/mq")]
    mq_pm = index.slice("mq-pm")
    assert mq_pm.stale is not None and "edited by hand" in mq_pm.stale
    assert mq_pm.mission == "mq-pm work"  # the last good entry, not the edit
    assert mq_pm.manager == "root-lead"
    assert mq_pm.known
    # The clusters it points at are still placed under it, stale through it.
    engine = index.slice("ex-engine-lead-427")
    assert engine.manager == "mq-pm"
    assert engine.stale is not None and "above this one" in engine.stale


def test_a_failing_cluster_keeps_its_agents_routable(tree):
    found = tree("org")
    before = refresh(found.paths, read=found.read).index.names()
    found.toml("hq/mq").write_text("this is not toml = = =\n")
    index = refresh(found.paths, read=found.read).index
    assert index.names() == before
    assert index.holds() == ()
    assert "does not parse" in index.slice("mq-roadmap-assistant").stale


def test_wiping_the_cache_during_a_failure_keeps_the_stale_entries(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "changed by hand"', seal=False)
    stale = slices(refresh(found.paths, read=found.read).index)
    shutil.rmtree(found.paths.cache)
    again = refresh(found.paths, read=found.read)
    assert slices(again.index) == stale
    assert again.changes == ()


def test_a_failure_with_no_last_good_entry_says_the_manager_is_unknown(tree):
    found = tree("org")
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "changed by hand"', seal=False)
    mq_pm = refresh(found.paths, read=found.read).index.slice("mq-pm")
    assert not mq_pm.known
    assert mq_pm.manager is None
    assert "edited by hand" in mq_pm.stale


def test_deleting_a_checkouts_folder_puts_its_names_on_hold(tree):
    found = tree("org")
    refresh(found.paths, read=found.read, now=1000.0)
    shutil.rmtree(found.root / "hq/mq/repos/engine")

    later = refresh(found.paths, read=found.read, now=2000.0)

    assert "ex-engine-lead-427" not in later.index.names()
    assert later.index.slice("ex-engine-lead-427") is None  # not live, not absent: held
    hold = later.index.hold("ex-engine-lead-427")
    assert hold.manager == "mq-pm"
    assert hold.removed_at == 2000.0
    assert hold.told == ()
    assert not hold.ended(2000.0 + HOLD_SECONDS - 1)
    assert hold.ended(2000.0 + HOLD_SECONDS)
    assert Change("held", "ex-engine-lead-427", old="mq-pm") in later.changes
    assert later.index.errors() == {}


def test_wiping_the_cache_during_a_hold_keeps_the_hold(tree):
    found = tree("org")
    refresh(found.paths, read=found.read, now=1000.0)
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    held = refresh(found.paths, read=found.read, now=2000.0).index.holds()
    shutil.rmtree(found.paths.cache)
    again = refresh(found.paths, read=found.read, now=3000.0)
    assert again.index.holds() == held
    assert again.changes == ()


def test_a_name_that_comes_back_leaves_its_hold(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    saved = found.root.parent / "saved-engine"
    shutil.move(found.root / "hq/mq/repos/engine", saved)
    assert refresh(found.paths, read=found.read).index.hold("ex-engine-lead-427")
    shutil.move(saved, found.root / "hq/mq/repos/engine")
    index = refresh(found.paths, read=found.read).index
    assert index.hold("ex-engine-lead-427") is None
    assert index.manager("ex-engine-lead-427") == "mq-pm"


def test_a_moved_checkout_keeps_its_names_and_raises_no_hold(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    shutil.move(found.root / "hq/mq/repos/engine", found.root / "hq/mq/repos/engine-moved")
    with (found.root / "registry").open("a") as registry:
        registry.write("hq/mq/repos/engine-moved\n")  # the old home stays registered
    later = refresh(found.paths, read=found.read)
    assert later.index.holds() == ()
    assert later.index.manager("ex-engine-lead-427") == "mq-pm"
    assert later.index.slice("ex-engine-lead-427").home == found.home("hq/mq/repos/engine-moved")
    assert [change.kind for change in later.changes] == []


def test_a_validation_failure_never_makes_a_name_held(tree):
    found = tree("two-managers")
    found.edit("hq", 'reports_at = ["repos/a"]\n', "", seal=True)  # only root-ops points: valid
    assert refresh(found.paths, read=found.read).index.manager("a-lead") == "root-ops"
    found.edit("hq", 'manager = "@maintainer"', 'manager = "@maintainer"\nreports_at = ["repos/a"]')
    index = refresh(found.paths, read=found.read).index
    assert index.holds() == ()
    assert index.slice("a-lead").manager == "root-ops"  # its last good entry
    assert "two explicit paths" in index.slice("a-lead").stale


def test_an_unavailable_helper_is_raised_not_read_as_an_empty_org(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)

    def broken():
        raise HelperUnavailable("the cluster helper (kodhama-cluster) is not installed")

    with pytest.raises(HelperUnavailable):
        refresh(found.paths, read=broken)


def test_no_clusters_and_no_state_writes_nothing(tree):
    found = tree("org")
    result = refresh(found.paths, read=lambda: [])
    assert result.index.names() == frozenset()
    assert not found.paths.state.exists()
    assert not found.paths.cache.exists()


def test_the_index_files_are_owner_only(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    for path in [found.paths.cache / "index.json", found.paths.state / "index" / "holds.json"]:
        assert path.stat().st_mode & 0o777 == 0o600
    assert (found.paths.state / "index").stat().st_mode & 0o777 == 0o700


def test_a_held_rebuild_lock_never_makes_a_caller_wait_past_its_bound(tree):
    # A hook must finish within its harness's timeout, so a caller that can't
    # get the lock builds the index in memory and writes nothing.
    found = tree("org")
    refresh(found.paths, read=found.read)
    found.edit("hq/mq/repos/engine", 'run = "running"', 'run = "parked"')
    cache = (found.paths.cache / "index.json").read_text()
    descriptor = os.open(found.paths.state / "index" / "lock", os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        result = refresh(found.paths, read=found.read, lock_wait=0.2)
        waited = time.monotonic() - started
    finally:
        os.close(descriptor)
    assert waited < 2
    assert result.index.run("ex-engine-lead-427") == "parked"
    assert result.changes == ()  # the holder of the lock reports them
    assert (found.paths.cache / "index.json").read_text() == cache


def test_a_hand_edit_with_no_last_good_entry_doesnt_fail_the_clusters_around_it(tree):
    found = tree("org")
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "changed by hand"', seal=False)
    index = refresh(found.paths, read=found.read).index
    assert list(index.errors()) == [found.home("hq/mq")]
    assert index.slice("root-lead").known
    assert index.slice("root-lead").stale is None
    engine = index.slice("ex-engine-lead-427")
    assert engine.manager == "mq-pm"
    assert engine.stale is not None and "above this one" in engine.stale


def test_an_unreadable_cluster_with_no_last_good_entry_leaves_the_rest_stale_not_wrong(tree):
    found = tree("org")
    found.toml("hq/mq").write_text("this is not toml = = =\n")
    index = refresh(found.paths, read=found.read).index
    assert list(index.errors()) == [found.home("hq/mq")]
    assert index.slice("root-lead").known
    assert any("can't be checked" in w for w in index.slice("hq-dashboard").warnings)
    engine = index.slice("ex-engine-lead-427")
    assert engine.manager is None
    assert engine.stale is not None and "can't be read" in engine.stale


def test_a_crash_while_writing_never_loses_a_hold(tree, monkeypatch):
    found = tree("org")
    refresh(found.paths, read=found.read)
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    real = index_module._write

    def crash_on_holds(path, data):
        if path.name == "holds.json":
            raise OSError("killed")
        real(path, data)

    monkeypatch.setattr(index_module, "_write", crash_on_holds)
    with pytest.raises(IndexUnavailable, match="killed"):
        refresh(found.paths, read=found.read)
    monkeypatch.setattr(index_module, "_write", real)
    assert refresh(found.paths, read=found.read).index.hold("ex-engine-lead-427").manager == "mq-pm"


def test_one_cluster_registered_under_two_paths_counts_once(tree):
    found = tree("internal-reports")
    os.symlink(found.root / "hq/team", found.root / "team-alias")
    with (found.root / "registry").open("a") as registry:
        registry.write("team-alias\n")
    index = refresh(found.paths, read=found.read).index
    assert index.errors() == {}
    assert index.manager("t-lead") == "root-lead"


def test_a_damaged_cache_or_snapshot_is_rebuilt_from_the_clusters(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    (found.paths.cache / "index.json").write_text("[]")
    for path in (found.paths.state / "index" / "last-good").glob("*.json"):
        path.write_text('{"format": 1, "home": "x"}')
    index = refresh(found.paths, read=found.read).index
    assert index.manager("mq-pm") == "root-lead"


def test_a_snapshot_that_isnt_text_is_damaged_and_rebuilt(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    for path in (found.paths.state / "index" / "last-good").glob("*.json"):
        path.write_bytes(b"\xff\xfe not utf-8")
    (found.paths.cache / "index.json").unlink()
    index = refresh(found.paths, read=found.read).index
    assert index.manager("mq-pm") == "root-lead"


def test_a_snapshot_that_is_gone_when_read_is_skipped(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    folder = found.paths.state / "index" / "last-good"
    (folder / "dangling.json").symlink_to(folder / "nowhere.json")
    (found.paths.cache / "index.json").unlink()
    index = refresh(found.paths, read=found.read).index
    assert index.manager("mq-pm") == "root-lead"


def test_damaged_hold_records_are_reported_never_dropped(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    (found.paths.state / "index" / "holds.json").write_text('{"format": 1, "holds": [{}]}')
    with pytest.raises(IndexUnavailable, match="holds.json"):
        refresh(found.paths, read=found.read)


def test_a_symlink_below_the_xdg_folder_is_refused(tree):
    found = tree("org")
    elsewhere = found.root.parent / "elsewhere"
    elsewhere.mkdir()
    found.paths.state.parent.mkdir(parents=True)
    os.symlink(elsewhere, found.paths.state)
    with pytest.raises(IndexUnavailable, match="symlink"):
        refresh(found.paths, read=found.read)
    assert list(elsewhere.iterdir()) == []


def test_records_read_before_a_newer_rebuild_never_overwrite_it(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)

    def read_then_lose_the_race():
        old = found.read()
        shutil.rmtree(found.root / "hq/mq/repos/engine")
        refresh(found.paths, read=found.read)  # another process, reading later
        return old

    late = refresh(found.paths, read=read_then_lose_the_race)
    assert not late.rebuilt
    assert late.index.hold("ex-engine-lead-427") is not None
    again = refresh(found.paths, read=found.read)
    assert again.index.hold("ex-engine-lead-427") is not None
    assert again.changes == ()


def test_a_new_reason_for_an_unreadable_cluster_is_shown(tree):
    found = tree("org")
    found.toml("hq/mq").write_text("this is not toml = = =\n")
    first = refresh(found.paths, read=found.read).index.errors()[found.home("hq/mq")]
    found.toml("hq/mq").write_text("schema = 9\n")
    second = refresh(found.paths, read=found.read).index.errors()[found.home("hq/mq")]
    assert first != second
    assert "schema" in second[0]


# Review round 1 (lead's review of da79b49, GRO-48 comment 42b48dd8).


def snapshot_bytes(found, relative):
    name = index_module._snapshot_name(found.home(relative))
    return (found.paths.state / "index" / "last-good" / name).read_bytes()


def test_a_graph_error_above_keeps_the_clusters_below_where_they_were_marked_stale(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    before = snapshot_bytes(found, "hq/mq")
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')  # sealed, so not a hand edit
    result = refresh(found.paths, read=found.read)
    assert found.home("hq") in result.index.errors()
    pm = result.index.slice("mq-pm")
    assert (pm.manager, pm.known, pm.orphan) == ("root-lead", True, False)
    assert pm.stale is not None and "above" in pm.stale
    assert result.index.slice("ex-engine-lead-427").stale is not None
    assert snapshot_bytes(found, "hq/mq") == before
    assert [change for change in result.changes if change.kind == "manager"] == []
    found.edit("hq", 'reports_at = ["mqq"]', 'reports_at = ["mq"]')
    fixed = refresh(found.paths, read=found.read)
    assert fixed.index.errors() == {}
    assert fixed.index.slice("mq-pm").stale is None
    assert fixed.changes == ()


def test_a_graph_error_with_no_last_good_marks_what_it_may_place_stale(tree):
    found = tree("org")
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    pm = refresh(found.paths, read=found.read).index.slice("mq-pm")
    assert pm.orphan
    assert pm.stale is not None  # it may sit below the failing cluster: never plain "no manager"


def test_names_known_only_as_unknown_are_held_when_they_leave(tree):
    found = tree("org")
    found.edit("hq/mq/repos/engine", 'mission = "ex-engine-lead work"', 'mission = "x"', seal=False)
    first = refresh(found.paths, read=found.read, now=1000.0)
    assert not first.index.slice("ex-engine-lead-427").known
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    later = refresh(found.paths, read=found.read, now=2000.0)
    hold = later.index.hold("ex-engine-lead-427")
    assert hold is not None and hold.manager is None
    assert Change("held", "ex-engine-lead-427", old_known=False) in later.changes


def test_a_held_name_that_returns_unknown_keeps_its_last_known_manager(tree):
    found = tree("org")
    refresh(found.paths, read=found.read, now=1000.0)
    saved = found.root.parent / "saved"
    shutil.move(found.root / "hq/mq/repos/engine", saved)
    refresh(found.paths, read=found.read, now=2000.0)
    shutil.move(saved, found.root / "hq/mq/repos/engine")
    found.edit("hq/mq/repos/engine", 'mission = "ex-engine-lead work"', 'mission = "x"', seal=False)
    back = refresh(found.paths, read=found.read, now=3000.0)
    assert back.index.hold("ex-engine-lead-427") is None
    assert not back.index.slice("ex-engine-lead-427").known
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    gone = refresh(found.paths, read=found.read, now=4000.0)
    hold = gone.index.hold("ex-engine-lead-427")
    assert hold is not None and hold.manager == "mq-pm"


def test_unknown_names_are_added_once_and_a_wipe_adds_nothing(tree):
    found = tree("org")
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "changed by hand"', seal=False)
    first = refresh(found.paths, read=found.read)
    assert Change("added", "mq-pm", new_known=False) in first.changes
    found.edit("hq/mq/repos/engine", 'run = "running"', 'run = "parked"')
    second = refresh(found.paths, read=found.read)
    assert second.rebuilt
    assert [change for change in second.changes if change.name == "mq-pm"] == []
    shutil.rmtree(found.paths.cache)
    third = refresh(found.paths, read=found.read)
    assert third.rebuilt and third.changes == ()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
def test_an_unwritable_state_folder_is_reported_and_nothing_is_written(tree):
    found = tree("org")
    found.paths.state.mkdir(parents=True)
    found.paths.state.chmod(0o500)
    try:
        with pytest.raises(IndexUnavailable):
            refresh(found.paths, read=found.read)
    finally:
        found.paths.state.chmod(0o700)
    assert list(found.paths.state.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through any mode")
def test_an_unwritable_cache_folder_is_reported_before_state_changes(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    holds = (found.paths.state / "index" / "holds.json").read_bytes()
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    found.paths.cache.chmod(0o500)
    try:
        with pytest.raises(IndexUnavailable):
            refresh(found.paths, read=found.read)
    finally:
        found.paths.cache.chmod(0o700)
    assert (found.paths.state / "index" / "holds.json").read_bytes() == holds


@pytest.mark.parametrize("key", ["cwd", "reports_at", "exclude"])
def test_a_nul_in_a_path_fails_only_its_own_cluster(tree, key):
    found = tree("org")
    records = found.read()
    mq = next(i for i, record in enumerate(records) if record.home == found.home("hq/mq"))
    declaration = dict(records[mq].declaration)
    agents = [dict(agent) for agent in declaration["agent"]]
    agents[0][key] = "a\x00b" if key == "cwd" else ["a\x00b"]
    declaration["agent"] = agents
    records[mq] = replace(records[mq], declaration=declaration)
    index = refresh(found.paths, read=lambda: records).index
    assert list(index.errors()) == [found.home("hq/mq")]
    assert "mq-pm" in index.names()
    assert index.slice("root-lead").stale is None


def test_a_cluster_registered_between_two_calls_is_routable_on_the_second(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    toml = found.toml("newcomer")
    toml.parent.mkdir(parents=True)
    toml.write_text(
        'schema = 1\n\n[[agent]]\nname = "new-lead"\nmanager = ""\nmission = "new work"\n'
    )
    subprocess.run([sys.executable, str(FAKE), "seal", str(toml)], check=True)
    with (found.root / "registry").open("a") as registry:
        registry.write("newcomer\n")
    result = refresh(found.paths, read=found.read)
    found_slice = result.index.slice("new-lead")
    assert found_slice is not None and found_slice.known and found_slice.orphan
    assert found_slice.stale is None
    assert Change("added", "new-lead") in result.changes


# Floor review of the round-1 fix (a2d7eb7).


def seal(found, relative):
    subprocess.run([sys.executable, str(FAKE), "seal", str(found.toml(relative))], check=True)


def test_a_cluster_a_rejected_declaration_lands_an_error_on_falls_back_and_fails(tree):
    # Fallbacks only grow: no cluster that fell back is cleared again (lead's ruling on
    # the third floor review). Failing for a neighbour's conflict is the known cost.
    found = tree("org")
    found.edit("hq/mq", 'reports_at = ["repos/*"]', 'reports_at = ["repos/engine"]')
    refresh(found.paths, read=found.read)
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mq", "mqq", "mq/repos/engine"]')
    result = refresh(found.paths, read=found.read)
    assert set(result.index.errors()) == {found.home("hq"), found.home("hq/mq/repos/engine")}
    engine = result.index.slice("ex-engine-lead-427")
    assert (engine.manager, engine.known) == ("mq-pm", True)
    assert engine.stale == "; ".join(result.index.errors()[found.home("hq/mq/repos/engine")])


def test_a_child_with_no_last_good_keeps_its_place_below_a_fallback(tree):
    found = tree("org")
    found.edit("hq/mq", 'reports_at = ["repos/*"]', 'reports_at = ["repos/engine"]')
    registry = found.root / "registry"
    registry.write_text("hq\nhq/mq\nhq/mq/repos/scratch\n")
    refresh(found.paths, read=found.read)
    registry.write_text("hq\nhq/mq\nhq/mq/repos/engine\nhq/mq/repos/scratch\n")
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mq", "mqq", "mq/repos/engine"]')
    engine = refresh(found.paths, read=found.read).index.slice("ex-engine-lead-427")
    assert (engine.manager, engine.known) == ("mq-pm", True)


def test_a_manager_change_made_while_stale_is_reported_once_current(tree):
    found = tree("org")
    with found.toml("hq/mq/repos/engine").open("a") as handle:
        handle.write('\n[[agent]]\nname = "eng-dev"\nmanager = "ex-engine-lead"\nmission = "d"\n')
    seal(found, "hq/mq/repos/engine")
    refresh(found.paths, read=found.read)
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    found.edit("hq/mq/repos/engine", 'manager = "ex-engine-lead"', 'manager = ""')
    during = refresh(found.paths, read=found.read)
    assert [change for change in during.changes if change.kind == "manager"] == []
    found.edit("hq", 'reports_at = ["mqq"]', 'reports_at = ["mq"]')
    fixed = refresh(found.paths, read=found.read)
    assert Change("manager", "eng-dev-427", old="ex-engine-lead-427", new="mq-pm") in fixed.changes


def test_a_swept_hold_doesnt_come_back_from_a_frozen_snapshot(tree):
    found = tree("org")
    refresh(found.paths, read=found.read, now=1000.0)
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    text = found.toml("hq/mq").read_text()
    start = text.index('[[agent]]\nname = "mq-roadmap-assistant"')
    found.toml("hq/mq").write_text(text[:start] + text[text.index("[[link]]", start) :])
    seal(found, "hq/mq")
    assert refresh(found.paths, read=found.read, now=2000.0).index.hold("mq-roadmap-assistant")
    holds = found.paths.state / "index" / "holds.json"
    holds.write_text('{"format": 1, "holds": []}')  # what sweep does once the hold ends
    found.edit("hq/mq/repos/engine", 'mission = "ex-engine-lead work"', 'mission = "y"')
    later = refresh(found.paths, read=found.read, now=900000.0)
    assert later.index.hold("mq-roadmap-assistant") is None
    assert later.changes == ()


def test_a_stale_cluster_with_no_last_good_gets_none_from_the_stale_view(tree):
    found = tree("org")
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    refresh(found.paths, read=found.read)
    folder = found.paths.state / "index" / "last-good"
    names = {path.name for path in folder.glob("*.json")}
    assert index_module._snapshot_name(found.home("hq/mq")) not in names


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")
def test_an_unreadable_state_folder_is_reported(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    folder = found.paths.state / "index"
    folder.chmod(0)
    try:
        with pytest.raises(IndexUnavailable):
            refresh(found.paths, read=lambda: [])
    finally:
        folder.chmod(0o700)


# Second floor review (5ab6494).


def _crossed(tmp_path, extras):
    """X points at W now, W pointed at X when last good, and W now holds a root head."""
    from mycelium.topology import CLUSTER_FILE, Declared

    root = tmp_path / f"crossed-{extras}"

    def home(name):
        path = root / name / CLUSTER_FILE
        path.parent.mkdir(parents=True)
        path.write_text("")
        return os.path.realpath(root / name)

    x, w = home("a-x"), home("b-w")
    current = {
        x: Declared(x, None, {"agent": [{"name": "x-lead", "reports_at": ["../b-w"]}]}),
        w: Declared(w, None, {"agent": [{"name": "w-lead", "manager": "@maintainer"}]}),
    }
    snapshots = {
        x: {"suffix": None, "declaration": {"agent": [{"name": "x-lead"}]}},
        w: {
            "suffix": None,
            "declaration": {"agent": [{"name": "w-lead", "reports_at": ["../a-x"]}]},
        },
    }
    for number in range(extras):
        other = home(f"c-{number}")
        agents = [{"name": f"e{number}", "manager": "@maintainer"}]
        current[other] = Declared(other, None, {"agent": agents})
    failing: dict = {}
    index_module._build([(each, None) for each in current], current, snapshots, failing)
    return {os.path.basename(each): reasons for each, reasons in failing.items()}


def test_the_fallback_settles_the_same_whatever_else_is_registered(tmp_path):
    results = [_crossed(tmp_path, extras) for extras in range(4)]
    assert all(result == results[0] for result in results)
    assert results[0], "a crossed pair of declarations fails somewhere"
    for reasons in results[0].values():
        assert reasons and not any("last good ones" in reason for reason in reasons)


def test_a_name_back_from_a_hold_while_stale_is_added_once_with_one_manager(tree):
    found = tree("org")
    refresh(found.paths, read=found.read, now=1000.0)
    shutil.rmtree(found.root / "hq/mq/repos/engine")
    refresh(found.paths, read=found.read, now=2000.0)  # held under mq-pm
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    with found.toml("hq/mq").open("a") as handle:
        handle.write(
            '\n[[agent]]\nname = "new-boss"\nmanager = "mq-pm"\nmission = "boss"\n'
            '\n[[agent]]\nname = "ex-engine-lead-427"\nmanager = "new-boss"\nmission = "back"\n'
        )
    seal(found, "hq/mq")
    back = refresh(found.paths, read=found.read, now=3000.0)
    added = [change for change in back.changes if change.name == "ex-engine-lead-427"]
    assert added == [Change("added", "ex-engine-lead-427", new="new-boss")]
    found.edit("hq", 'reports_at = ["mqq"]', 'reports_at = ["mq"]')
    fixed = refresh(found.paths, read=found.read, now=4000.0)
    assert [change for change in fixed.changes if change.name == "ex-engine-lead-427"] == []


def test_a_manager_learned_while_stale_is_reported_once_current(tree):
    found = tree("org")
    found.edit("hq/mq/repos/engine", 'mission = "ex-engine-lead work"', 'mission = "x"', seal=False)
    refresh(found.paths, read=found.read)  # published as unknown
    found.edit("hq", 'reports_at = ["mq"]', 'reports_at = ["mqq"]')
    seal(found, "hq/mq/repos/engine")
    during = refresh(found.paths, read=found.read)
    assert [change for change in during.changes if change.name == "ex-engine-lead-427"] == []
    found.edit("hq", 'reports_at = ["mqq"]', 'reports_at = ["mq"]')
    fixed = refresh(found.paths, read=found.read)
    expected = Change("manager", "ex-engine-lead-427", new="mq-pm", old_known=False)
    assert expected in fixed.changes


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")
def test_an_unreadable_last_good_folder_is_reported(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    folder = found.paths.state / "index" / "last-good"
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "by hand"', seal=False)
    folder.chmod(0o300)
    try:
        with pytest.raises(IndexUnavailable):
            refresh(found.paths, read=found.read)
    finally:
        folder.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads through any mode")
def test_an_unreadable_snapshot_is_reported_and_kept(tree):
    found = tree("org")
    refresh(found.paths, read=found.read)
    path = (
        found.paths.state / "index" / "last-good" / index_module._snapshot_name(found.home("hq/mq"))
    )
    found.edit("hq/mq", 'mission = "mq-pm work"', 'mission = "by hand"', seal=False)
    path.chmod(0)
    try:
        with pytest.raises(IndexUnavailable):
            refresh(found.paths, read=found.read)
    finally:
        path.chmod(0o600)
    assert path.exists()


# Third floor review (42af0d6).


def _plain(tmp_path, *names):
    from mycelium.paths import Paths
    from mycelium.topology import CLUSTER_FILE

    homes = []
    for name in names:
        path = tmp_path / name / CLUSTER_FILE
        path.parent.mkdir(parents=True)
        path.write_text("")
        homes.append(os.path.realpath(tmp_path / name))
    paths = Paths(tmp_path / "state", tmp_path / "data", tmp_path / "cache", tmp_path / "x")
    return paths, homes


def _records(*pairs):
    from mycelium.clusters import ClusterRecord

    return lambda: [ClusterRecord(home, "ok", None, repr(d), repr(d), None, d) for home, d in pairs]


def _agents(*agents):
    return {
        "agent": [
            dict(zip(("name", "manager", "reports_at"), each, strict=False)) for each in agents
        ]
    }


def test_a_clash_between_two_clusters_that_fall_back_is_reported_on_both(tmp_path):
    paths, (a, b) = _plain(tmp_path, "a", "b")
    refresh(
        paths,
        read=_records(
            (a, _agents(("a-lead", "@maintainer"))), (b, _agents(("b-lead", "@maintainer")))
        ),
    )
    a2 = _agents(("a-lead", "@maintainer"), ("dup", "a-lead"))
    b2 = _agents(("b-lead", "@maintainer"), ("dup", "b-lead"))
    errors = refresh(paths, read=_records((a, a2), (b, b2))).index.errors()
    assert set(errors) == {a, b}
    assert all(any("dup" in reason for reason in errors[home]) for home in (a, b))


def test_a_cluster_below_one_that_fell_back_for_a_clash_is_stale_through_it(tmp_path):
    paths, (h, c, y) = _plain(tmp_path, "h", "c", "y")
    head = ("h-lead", "@maintainer")
    h1 = _agents(head, ("boss1", "h-lead", ["../c"]), ("boss2", "h-lead"), ("dup", "h-lead"))
    h2 = _agents(head, ("boss1", "h-lead"), ("boss2", "h-lead", ["../c"]), ("dup", "h-lead"))
    c1 = {"agent": [{"name": "c-lead"}]}
    y1 = _agents(("y-lead", "@maintainer"))
    y2 = _agents(("y-lead", "@maintainer"), ("dup", "y-lead"))
    refresh(paths, read=_records((h, h1), (c, c1), (y, y1)))
    index = refresh(paths, read=_records((h, h2), (c, c1), (y, y2))).index
    lead = index.slice("c-lead")
    assert c not in index.errors()
    assert lead.manager == "boss1" and "above" in lead.stale


def test_a_fallback_lists_each_reason_once(tmp_path):
    paths, homes = _plain(tmp_path, "a", "b", "c")
    refresh(
        paths,
        read=_records(
            *((h, _agents((f"{n}-lead", "@maintainer"))) for h, n in zip(homes, "abc", strict=True))
        ),
    )
    clash = [
        (h, _agents((f"{n}-lead", "@maintainer"), ("dup", f"{n}-lead")))
        for h, n in zip(homes, "abc", strict=True)
    ]
    errors = refresh(paths, read=_records(*clash)).index.errors()
    assert set(errors) == set(homes)
    assert all(len(set(reasons)) == len(reasons) for reasons in errors.values())


@pytest.mark.parametrize("name", ["holds.json", "published.json"])
def test_a_state_file_that_isnt_text_is_reported_as_damaged(tree, name):
    found = tree("org")
    refresh(found.paths, read=found.read)
    (found.paths.state / "index" / name).write_bytes(b"\xff\xfe not utf-8")
    (found.paths.cache / "index.json").unlink()
    with pytest.raises(IndexUnavailable, match="damaged"):
        refresh(found.paths, read=found.read)
