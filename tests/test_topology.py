"""The hostile-topology cases (hq-resume requirements, success test 4), one per
fixture under tests/fixtures/clusters/, and the cluster spec's validation (§5.1)."""

import json
from pathlib import Path

import pytest

from mycelium.index import refresh

FIXTURES = Path(__file__).parent / "fixtures" / "clusters"

CASES = sorted(path.name for path in FIXTURES.iterdir() if (path / "expected.json").is_file())


def view(slice_, key):
    value = getattr(slice_, key)
    if key == "peers":
        return [[peer.name, peer.topic] for peer in value]
    if key in ("interested", "follows"):
        return [[party.name, list(party.events)] for party in value]
    if key == "escalation":
        return [[route.to, list(route.when)] for route in value]
    if key == "reports":
        return list(value)
    return value


@pytest.mark.parametrize("case", CASES)
def test_each_fixture_gives_its_defined_result(tree, case):
    found = tree(case)
    expected = json.loads((FIXTURES / case / "expected.json").read_text())

    index = refresh(found.paths, read=found.read).index

    assert sorted(index.names()) == expected["names"]
    errors = {found.relative(home): reasons for home, reasons in index.errors().items()}
    assert sorted(errors) == sorted(expected["errors"])
    for home, wanted in expected["errors"].items():
        for part in wanted:
            assert any(part in reason for reason in errors[home]), (home, part, errors[home])
    assert [hold.name for hold in index.holds()] == expected.get("holds", [])
    for name, wanted in expected["slices"].items():
        got = index.slice(name)
        assert got is not None, name
        for key, value in wanted.items():
            if key == "stale" and value is not None:
                assert got.stale is not None and value in got.stale, (name, got.stale)
            else:
                assert view(got, key) == value, (name, key)


def errors_of(found, relative):
    index = refresh(found.paths, read=found.read).index
    return index, index.errors().get(found.home(relative), ())


def test_a_manager_that_names_no_agent_in_the_cluster_fails_it(tree):
    found = tree("internal-reports")
    found.edit("hq/team", 'manager = "t-dev"', 'manager = "t-nobody"')
    index, errors = errors_of(found, "hq/team")
    assert any("t-nobody" in reason for reason in errors)
    assert index.slice("t-qa").stale


def test_a_cwd_that_leaves_the_home_fails_the_cluster(tree):
    found = tree("internal-reports")
    found.edit("hq/team", 'name = "t-qa"', 'name = "t-qa"\ncwd = "../.."')
    _, errors = errors_of(found, "hq/team")
    assert any("cwd" in reason for reason in errors)


def test_a_running_agent_under_a_parked_one_fails_its_cluster(tree):
    found = tree("internal-reports")
    found.edit(
        "hq/team",
        'run = "running"\nmission = "t-dev work"',
        'run = "parked"\nmission = "t-dev work"',
    )
    _, errors = errors_of(found, "hq/team")
    assert any("t-qa" in reason and "not running" in reason for reason in errors)


def test_a_link_to_an_unknown_name_fails_the_cluster_it_lives_in(tree):
    found = tree("org")
    found.edit("hq/mq", 'to = "hq-dashboard"', 'to = "hq-nobody"')
    _, errors = errors_of(found, "hq/mq")
    assert any("hq-nobody" in reason for reason in errors)


def test_a_second_peer_link_for_one_pair_fails(tree):
    found = tree("org")
    found.edit(
        "hq",
        "[[link]]",
        '[[link]]\nkind = "peer"\nfrom = "hq-dashboard"\nto = "mq-pm"\ntopic = "again"\n\n[[link]]',
    )
    index = refresh(found.paths, read=found.read).index
    assert index.errors(), "a second peer link between one pair is an error"


def test_a_link_missing_its_field_fails(tree):
    found = tree("org")
    found.edit("hq/mq", 'topic = "fleet status for MQ tracks"', "")
    _, errors = errors_of(found, "hq/mq")
    assert any("topic" in reason for reason in errors)


def test_an_unknown_event_kind_is_a_warning_on_the_subscriber_only(tree):
    found = tree("org")
    found.edit("hq", '"blocked"]', '"blocked", "gossip"]')
    index = refresh(found.paths, read=found.read).index
    assert index.errors() == {}
    assert any("gossip" in warning for warning in index.slice("hq-dashboard").warnings)
    assert index.slice("mq-pm").warnings == ()


def test_two_clusters_declaring_one_effective_name_both_fail(tree):
    found = tree("unpointed")
    found.edit("hq/lone", 'name = "lone-dev"', 'name = "hq-lead"')
    index = refresh(found.paths, read=found.read).index
    assert found.home("hq") in index.errors()
    assert found.home("hq/lone") in index.errors()


def test_a_suffix_makes_every_name_in_the_checkout_effective(tree):
    found = tree("org")
    index = refresh(found.paths, read=found.read).index
    engine = index.slice("ex-engine-lead-427")
    assert engine.declared_name == "ex-engine-lead"
    assert index.slice("ex-engine-lead") is None


def test_a_link_to_a_declared_name_matches_every_instance(tree):
    found = tree("external-worktree")
    found.edit(
        "hq",
        'reports_at = ["repos/*"]',
        'reports_at = ["repos/*"]\n\n[[link]]\nkind = "interested"\nfrom = "hq-lead"\n'
        'to = "app-lead"\nevents = ["done"]',
    )
    index = refresh(found.paths, read=found.read).index
    follows = [party.name for party in index.slice("hq-lead").follows]
    assert follows == ["app-lead-101", "app-lead-102"]
    assert [party.name for party in index.slice("app-lead-102").interested] == ["hq-lead"]
