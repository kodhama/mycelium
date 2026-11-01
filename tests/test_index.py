"""The index's revision check, cache, last-good entries and holds (hub plan U14)."""

import fcntl
import os
import shutil
import time

import pytest

from mycelium import index as index_module
from mycelium.clusters import HelperUnavailable
from mycelium.index import HAND_EDITED, HOLD_SECONDS, Change, IndexUnavailable, refresh


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
    with pytest.raises(OSError):
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
