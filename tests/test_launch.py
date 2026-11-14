"""The launch-name rule (R2, R3; hub plan U4 approach, lessons C2 and T9)."""

import json
import time

import pytest
from test_pane import HERDR, SESSION, agent_record, pane, process, process_info

from mycelium import launch
from mycelium.identity import Caller

PID = 4100
CLAUDE = Caller("claude", PID, SESSION, False, "walk", None)
NAMED = pane("w1:p1", agent="claude", session=SESSION, title="a title")
STATE = {
    "panes": [NAMED],
    "agents": [agent_record(NAMED, "ogham-worker")],
    "process_info": [process_info("w1:p1", 4000, process(PID, "claude"))],
}


@pytest.fixture
def herdr(tmp_path, monkeypatch):
    """The fake herdr; `herdr(**changes)` replaces parts of STATE, `herdr.calls()` lists calls."""
    state = tmp_path / "herdr-state.json"
    log = tmp_path / "herdr-calls.log"
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.delenv("FAKE_HERDR_FAIL", raising=False)

    def with_state(**changes):
        state.write_text(json.dumps(STATE | changes))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    with_state()
    with_state.calls = calls
    return with_state


def resolve(caller=CLAUDE, harness=None, env=None, **kwargs):
    return launch.resolve(caller, harness, env or {}, command=HERDR, **kwargs)


def test_every_source_agreeing_gives_the_name(herdr):
    found = resolve(harness="ogham-worker", env={"KODHAMA_AGENT": "ogham-worker"})
    assert found.name == "ogham-worker"
    assert found.reason is None
    assert found.names == {
        "harness": "ogham-worker",
        "environment": "ogham-worker",
        "pane": "ogham-worker",
    }
    assert found.herdr == "named"


@pytest.mark.parametrize(
    ("harness", "env"),
    [("ogham-worker", {}), (None, {"KODHAMA_AGENT": "ogham-worker"})],
)
def test_one_source_alone_gives_the_name(herdr, harness, env):
    herdr(agents=[])  # herdr holds no agent record yet (lessons C2)
    found = resolve(harness=harness, env=env)
    assert found.name == "ogham-worker"
    assert found.herdr == "none"


def test_the_herdr_name_alone_gives_the_name(herdr):
    assert resolve().name == "ogham-worker"


@pytest.mark.parametrize(
    ("harness", "env"),
    [
        ("ogham-worker", {"KODHAMA_AGENT": "ogham-lead"}),
        ("ogham-lead", {}),  # the harness name against herdr's
        (None, {"KODHAMA_AGENT": "ogham-lead"}),  # the environment against herdr's
    ],
)
def test_sources_that_disagree_give_no_name(herdr, harness, env):
    found = resolve(harness=harness, env=env)
    assert found.name is None
    assert "disagree" in found.reason
    assert "ogham-lead" in found.reason


def test_no_source_gives_no_name(herdr):
    herdr(agents=[])
    found = resolve()
    assert found.name is None
    assert found.reason.startswith("no launch name")


def test_a_herdr_record_for_another_session_does_not_count(herdr):
    # The pane holds the pid, but herdr's record is another session's (a stale /clear).
    stale = pane("w1:p1", agent="claude", session="another-session")
    herdr(panes=[stale], agents=[agent_record(stale, "ogham-lead")])
    found = resolve(env={"KODHAMA_AGENT": "ogham-worker"})
    assert found.name == "ogham-worker"
    assert found.herdr == "stale"
    assert "pane" not in found.names


def test_a_pane_whose_foreground_does_not_hold_the_pid_does_not_count(herdr):
    herdr(process_info=[process_info("w1:p1", 4000, process(PID + 1, "claude"))])
    found = resolve(env={"KODHAMA_AGENT": "ogham-worker"})
    assert found.name == "ogham-worker"
    assert found.herdr == "none"


def test_herdr_failing_is_unknown_never_a_match(herdr, monkeypatch):
    monkeypatch.setenv("FAKE_HERDR_FAIL", "exit")
    found = resolve(env={"KODHAMA_AGENT": "ogham-worker"})
    assert found.name == "ogham-worker"
    assert found.herdr == "unknown"
    assert "pane" not in found.names
    alone = resolve()
    assert alone.name is None
    assert "herdr could not be read" in alone.reason


def test_a_stalled_herdr_is_cut_off_within_one_deadline(herdr):
    herdr(slow=[["pane", "process-info", "--pane", "w1:p1"]])
    started = time.monotonic()
    found = resolve(env={"KODHAMA_AGENT": "ogham-worker"}, timeout=1.0)
    assert time.monotonic() - started < 1.9
    assert found.herdr == "unknown"
    assert found.name == "ogham-worker"


def test_the_deadline_covers_the_scan_and_both_identity_reads(herdr):
    # Each read answers in time on its own; together they outlast the one deadline.
    delayed = [[["pane", "get", "w1:p1"], 0.8], [["agent", "get", "w1:p1"], 0.8]]
    herdr(delayed=delayed)
    started = time.monotonic()
    found = resolve(timeout=1.2)
    assert time.monotonic() - started < 1.6
    assert found.herdr == "unknown"


def test_the_pane_title_never_counts(herdr):
    titled = pane("w1:p1", agent="claude", session=SESSION, title="ogham-worker")
    herdr(panes=[titled], agents=[])
    found = resolve()
    assert found.name is None
    assert found.herdr == "none"


def test_a_caller_with_no_pid_is_not_looked_up_in_herdr(herdr):
    tui = Caller("codex", None, "thread-1", None, "walk", "a Codex TUI caller")
    found = resolve(caller=tui, env={"KODHAMA_AGENT": "ogham-worker"})
    assert found.name == "ogham-worker"
    assert found.herdr == "unchecked"
    assert herdr.calls() == []


@pytest.mark.parametrize("where", ["harness", "environment", "herdr"])
def test_the_maintainer_is_never_a_launch_name(herdr, where):
    if where == "herdr":
        herdr(agents=[agent_record(NAMED, "@maintainer")])
        found = resolve()
    elif where == "harness":
        herdr(agents=[])
        found = resolve(harness="@maintainer")
    else:
        herdr(agents=[])
        found = resolve(env={"KODHAMA_AGENT": "@maintainer"})
    assert found.name is None
    assert "R35" in found.reason


def test_an_empty_environment_value_is_no_name(herdr):
    herdr(agents=[])
    assert resolve(env={"KODHAMA_AGENT": ""}).name is None


def test_disagrees_with_names_any_source_that_differs(herdr):
    found = resolve(env={"KODHAMA_AGENT": "ogham-worker"})
    assert not found.disagrees_with("ogham-worker")
    assert found.disagrees_with("ogham-lead")
