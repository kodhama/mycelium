import json
import sys
import time
from pathlib import Path

import pytest

from mycelium.pane import (
    ForegroundProcess,
    PaneIdentity,
    PaneUnavailable,
    find_pane,
    foreground,
    foreground_is,
    read_identity,
)

FAKE = Path(__file__).parent / "fake_herdr.py"
HERDR = [sys.executable, str(FAKE)]

CWD = "/home/maintainer/project"
SESSION = "00000000-0000-0000-0000-000000000001"


def pane(pane_id: str, *, agent: str | None = None, session: str | None = None, title="") -> dict:
    """A pane record in herdr 0.9.3's shape; an agent pane carries `agent` and `agent_session`."""
    record = {
        "agent_status": "unknown" if agent is None else "working",
        "cwd": CWD,
        "focused": False,
        "foreground_cwd": CWD,
        "pane_id": pane_id,
        "revision": 1,
        "scroll": {"max_offset_from_bottom": 0, "offset_from_bottom": 0, "viewport_rows": 40},
        "tab_id": pane_id.split(":")[0] + ":t1",
        "terminal_id": "term_" + pane_id.replace(":", "_"),
        "terminal_title": f"* {title}",
        "terminal_title_stripped": title,
        "workspace_id": pane_id.split(":")[0],
    }
    if agent is not None:
        record["agent"] = agent
        record["agent_session"] = {
            "agent": agent,
            "kind": "id",
            "source": f"herdr:{agent}",
            "value": session,
        }
    return record


def agent_record(pane_record: dict, name: str) -> dict:
    """An agent record: the pane record plus the live name and its sequence fields."""
    record = {k: v for k, v in pane_record.items() if k != "scroll"}
    return record | {"interactive_ready": True, "name": name, "state_change_seq": 7}


def process(pid: int, name: str, *argv: str, cwd: str | None = CWD) -> dict:
    return {
        "argv": [name, *argv],
        "argv0": name,
        "cmdline": " ".join([name, *argv]),
        "cwd": cwd,
        "name": name,
        "pid": pid,
    }


def process_info(pane_id: str, shell_pid: int, *processes: dict) -> dict:
    return {
        "foreground_process_group_id": processes[0]["pid"] if processes else shell_pid,
        "foreground_processes": list(processes),
        "pane_id": pane_id,
        "shell_pid": shell_pid,
    }


BOUND = pane("w1:p1", agent="claude", session=SESSION, title="role-01")
STARTING = pane("w1:p2", agent="claude", session="00000000-0000-0000-0000-000000000002")
SHELL = pane("w2:p1", title="maintainer@host:~")
RESTORED = {  # herdr lists a pane it could not restore, with no live process behind it
    "agent_status": "unknown",
    "cwd": CWD,
    "focused": False,
    "pane_id": "w3:p1",
    "restore_error": "the session could not be restored",
    "revision": 0,
    "tab_id": "w3:t1",
    "terminal_id": "term_w3_p1",
    "workspace_id": "w3",
}

STATE = {
    "panes": [RESTORED, SHELL, BOUND, STARTING],
    "agents": [agent_record(BOUND, "role-01")],
    "process_info": [
        process_info(
            "w1:p1",
            1000,
            process(1100, "caffeinate", "-i", "-t", "300"),
            process(1001, "claude", "-n", "role-01"),
        ),
        process_info("w1:p2", 2000, process(2001, "claude", cwd="/home/maintainer/other")),
        process_info("w2:p1", 3000, process(3000, "zsh")),
    ],
}


@pytest.fixture
def herdr(tmp_path, monkeypatch):
    """The fake herdr over STATE; `herdr.calls()` lists the arguments of each call made."""
    state = tmp_path / "herdr-state.json"
    state.write_text(json.dumps(STATE))
    log = tmp_path / "herdr-calls.log"
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))
    monkeypatch.setenv("FAKE_HERDR_LOG", str(log))
    monkeypatch.delenv("FAKE_HERDR_FAIL", raising=False)

    class Herdr:
        command = HERDR

        @staticmethod
        def calls() -> list[list[str]]:
            if not log.exists():
                return []
            return [json.loads(line) for line in log.read_text().splitlines()]

    return Herdr


# Scenario 1: reading a pane's identity.


def test_a_bound_panes_identity_carries_its_record_and_the_agents_live_name(herdr):
    assert read_identity("w1:p1", command=HERDR) == PaneIdentity(
        pane_id="w1:p1",
        agent="claude",
        agent_session_id=SESSION,
        title="role-01",
        cwd=CWD,
        terminal_id="term_w1_p1",
        agent_name="role-01",
    )


def test_a_pane_herdr_holds_no_agent_record_for_yet_has_no_agent_name(herdr):
    # Lessons C2: at SessionStart the agent record may not exist yet.
    found = read_identity("w1:p2", command=HERDR)
    assert found.agent == "claude"
    assert found.agent_session_id == "00000000-0000-0000-0000-000000000002"
    assert found.agent_name is None


def test_a_plain_shell_pane_has_no_agent(herdr):
    found = read_identity("w2:p1", command=HERDR)
    assert (found.agent, found.agent_session_id, found.agent_name) == (None, None, None)
    assert found.title == "maintainer@host:~"


def test_a_pane_herdr_does_not_know_reads_as_none(herdr):
    assert read_identity("w0:p0", command=HERDR) is None


# Scenario 2: the foreground check (T9).


def test_the_foreground_lists_each_process_herdr_reports(herdr):
    assert foreground("w1:p1", command=HERDR) == [
        ForegroundProcess(
            pid=1100, name="caffeinate", argv=("caffeinate", "-i", "-t", "300"), cwd=CWD
        ),
        ForegroundProcess(pid=1001, name="claude", argv=("claude", "-n", "role-01"), cwd=CWD),
    ]


def test_the_foreground_of_an_unknown_pane_is_empty(herdr):
    assert foreground("w0:p0", command=HERDR) == []
    assert foreground("w3:p1", command=HERDR) == []


def test_the_foreground_check_holds_only_for_a_listed_pid(herdr):
    assert foreground_is("w1:p1", 1001, command=HERDR)
    assert foreground_is("w1:p1", 1001, name="claude", cwd=CWD, command=HERDR)
    assert not foreground_is("w1:p1", 2001, command=HERDR)
    assert not foreground_is("w0:p0", 1001, command=HERDR)


def test_the_foreground_check_fails_when_the_name_or_cwd_differs(herdr):
    assert not foreground_is("w1:p1", 1001, name="codex", command=HERDR)
    assert not foreground_is("w1:p1", 1001, cwd="/home/maintainer/other", command=HERDR)


# Scenario 3: finding a pane for a pid.


def test_a_hint_whose_foreground_holds_the_pid_is_used_without_a_scan(herdr):
    assert find_pane(1001, hint="w1:p1", command=HERDR) == "w1:p1"
    assert ["pane", "list"] not in herdr.calls()


def test_a_wrong_hint_falls_through_to_the_scan(herdr):
    # An inherited HERDR_PANE_ID names the parent's pane, not this process's one.
    assert find_pane(2001, hint="w1:p1", command=HERDR) == "w1:p2"
    assert herdr.calls()[0] == ["pane", "process-info", "--pane", "w1:p1"]


def test_a_hint_naming_no_pane_falls_through_to_the_scan(herdr):
    assert find_pane(1001, hint="w0:p0", command=HERDR) == "w1:p1"


def test_without_a_hint_the_scan_finds_the_pane(herdr):
    assert find_pane(3000, command=HERDR) == "w2:p1"


def test_a_pid_no_pane_holds_has_no_pane_even_with_a_hint(herdr):
    assert find_pane(9999, hint="w1:p1", command=HERDR) is None
    assert find_pane(9999, command=HERDR) is None


# Scenario 4: every herdr failure is one PaneUnavailable.

CALLS = [
    pytest.param(
        lambda command, **kw: read_identity("w1:p1", command=command, **kw), id="identity"
    ),
    pytest.param(lambda command, **kw: foreground("w1:p1", command=command, **kw), id="foreground"),
    pytest.param(
        lambda command, **kw: foreground_is("w1:p1", 1001, command=command, **kw), id="check"
    ),
    pytest.param(lambda command, **kw: find_pane(1001, command=command, **kw), id="find"),
]


@pytest.mark.parametrize("call", CALLS)
def test_a_missing_herdr_is_reported_plainly(tmp_path, call):
    with pytest.raises(PaneUnavailable, match="not installed"):
        call([str(tmp_path / "no-such-herdr")])


@pytest.mark.parametrize("call", CALLS)
def test_a_herdr_that_hangs_is_cut_off(herdr, monkeypatch, call):
    monkeypatch.setenv("FAKE_HERDR_FAIL", "sleep")
    with pytest.raises(PaneUnavailable, match="did not answer"):
        call(HERDR, timeout=0.5)


@pytest.mark.parametrize("call", CALLS)
def test_a_failing_herdr_is_reported_with_what_it_said(herdr, monkeypatch, call):
    monkeypatch.setenv("FAKE_HERDR_FAIL", "exit")
    with pytest.raises(PaneUnavailable, match="exited 3: herdr: something went wrong"):
        call(HERDR)


@pytest.mark.parametrize("call", CALLS)
def test_a_herdr_error_that_is_not_a_missing_pane_is_reported(herdr, monkeypatch, call):
    monkeypatch.setenv("FAKE_HERDR_FAIL", "error")
    with pytest.raises(PaneUnavailable, match="exited 1: the herdr server is not running"):
        call(HERDR)


@pytest.mark.parametrize("call", CALLS)
def test_a_herdr_that_prints_no_json_is_refused(herdr, monkeypatch, call):
    monkeypatch.setenv("FAKE_HERDR_FAIL", "garbage")
    with pytest.raises(PaneUnavailable, match="unexpected output"):
        call(HERDR)


def script(tmp_path: Path, output: str) -> list[str]:
    path = tmp_path / "herdr.py"
    path.write_text(f"print({output!r})\n")
    return [sys.executable, str(path)]


@pytest.mark.parametrize(
    "output",
    [
        "[]",
        '{"id": "cli:pane:get"}',
        '{"id": "cli:pane:get", "result": []}',
        '{"id": "cli:pane:get", "result": {"type": "pane_info"}}',
        '{"id": "cli:pane:get", "result": {"pane": {"agent": "claude"}}}',
        '{"id": "cli:pane:get", "result": {"pane": {"pane_id": "w1:p1", "agent": 7}}}',
        '{"id": "cli:pane:get", "result": {"pane": {"pane_id": "w1:p1", "agent_session": "x"}}}',
    ],
)
def test_a_pane_record_that_is_not_the_shape_is_refused(tmp_path, output):
    with pytest.raises(PaneUnavailable, match="unexpected output"):
        read_identity("w1:p1", command=script(tmp_path, output))


@pytest.mark.parametrize(
    "output",
    [
        '{"id": "cli:pane:process_info", "result": {"process_info": {}}}',
        '{"id": "cli:pane:process_info", "result": {"process_info": {"foreground_processes": {}}}}',
        '{"result": {"process_info": {"foreground_processes": [{"pid": "1", "name": "zsh",'
        ' "argv": ["zsh"]}]}}}',
        '{"result": {"process_info": {"foreground_processes": [{"pid": 1, "argv": ["zsh"]}]}}}',
        '{"result": {"process_info": {"foreground_processes": [{"pid": 1, "name": "zsh",'
        ' "argv": "zsh"}]}}}',
        '{"result": {"process_info": {"foreground_processes": [{"pid": true, "name": "zsh",'
        ' "argv": ["zsh"]}]}}}',
    ],
)
def test_process_info_that_is_not_the_shape_is_refused(tmp_path, output):
    with pytest.raises(PaneUnavailable, match="unexpected output"):
        foreground("w1:p1", command=script(tmp_path, output))


def test_a_pane_list_that_is_not_the_shape_is_refused(tmp_path):
    output = '{"id": "cli:pane:list", "result": {"panes": [{"cwd": "/"}]}}'
    with pytest.raises(PaneUnavailable, match="unexpected output"):
        find_pane(1001, command=script(tmp_path, output))


# Review fixes (GRO-49).
def with_state(tmp_path, monkeypatch, **changes) -> None:
    state = tmp_path / "herdr-state-changed.json"
    state.write_text(json.dumps(STATE | changes))
    monkeypatch.setenv("FAKE_HERDR_STATE", str(state))


def test_one_unreadable_pane_does_not_stop_the_scan(herdr, tmp_path, monkeypatch):
    # w2:p1 is listed before the pane that holds the pid, and its read fails.
    with_state(tmp_path, monkeypatch, broken=[["pane", "process-info", "--pane", "w2:p1"]])
    assert find_pane(1001, command=HERDR) == "w1:p1"


def test_a_scan_that_finds_nothing_after_skipping_a_pane_says_so(herdr, tmp_path, monkeypatch):
    # "No pane holds it" would be a guess when one pane could not be read.
    with_state(tmp_path, monkeypatch, broken=[["pane", "process-info", "--pane", "w2:p1"]])
    with pytest.raises(PaneUnavailable, match="w2:p1"):
        find_pane(9999, command=HERDR)


def test_an_agent_lookup_error_other_than_not_found_is_reported(herdr, tmp_path, monkeypatch):
    with_state(tmp_path, monkeypatch, broken=[["agent", "get", "w1:p1"]])
    with pytest.raises(PaneUnavailable, match="read failed"):
        read_identity("w1:p1", command=HERDR)


@pytest.mark.parametrize("agent_session", ["00000000-0000-0000-0000-0000000000ff", None])
def test_an_agent_record_for_another_session_gives_no_agent_name(
    herdr, tmp_path, monkeypatch, agent_session
):
    # pane get and agent get are two calls: a pane replaced between them, or a record left
    # stale by /clear, must not pair one session's id with another's name.
    stale = agent_record(BOUND, "role-02")
    if agent_session is None:
        del stale["agent_session"]
    else:
        stale["agent_session"] = stale["agent_session"] | {"value": agent_session}
    with_state(tmp_path, monkeypatch, agents=[stale])
    identity = read_identity("w1:p1", command=HERDR)
    assert (identity.agent_session_id, identity.agent_name) == (SESSION, None)


def test_empty_values_read_as_none(herdr, tmp_path, monkeypatch):
    blank = pane("w4:p1", agent="claude", session="", title="")
    with_state(tmp_path, monkeypatch, panes=[blank], agents=[agent_record(blank, "")])
    identity = read_identity("w4:p1", command=HERDR)
    assert (identity.title, identity.agent_session_id, identity.agent_name) == (None, None, None)


def test_an_unreadable_hint_falls_through_to_the_scan(herdr, tmp_path, monkeypatch):
    with_state(tmp_path, monkeypatch, broken=[["pane", "process-info", "--pane", "w2:p1"]])
    assert find_pane(1001, hint="w2:p1", command=HERDR) == "w1:p1"


def test_a_scan_that_finds_nothing_names_why_each_pane_was_unreadable(herdr, tmp_path, monkeypatch):
    with_state(tmp_path, monkeypatch, broken=[["pane", "process-info", "--pane", "w2:p1"]])
    with pytest.raises(PaneUnavailable, match=r"w2:p1 \(.*read failed"):
        find_pane(9999, command=HERDR)


def test_a_stalled_herdr_ends_the_scan_within_one_timeout(herdr, tmp_path, monkeypatch):
    # A hook runs the scan inside its harness's timeout: the whole scan shares one.
    slow = [["pane", "process-info", "--pane", pane_id] for pane_id in ("w2:p1", "w1:p1")]
    with_state(tmp_path, monkeypatch, slow=slow)
    started = time.monotonic()
    with pytest.raises(PaneUnavailable):
        find_pane(2001, command=HERDR, timeout=1.0)
    assert time.monotonic() - started < 1.9


def test_a_scan_of_slow_panes_shares_one_deadline(herdr, tmp_path, monkeypatch):
    # Each pane read answers after 1.2 s, so no single read outlasts a 2 s timeout. The
    # second read starts after the first ends, past 1.2 s: a shared deadline cuts it at 2 s,
    # while a read given its own full timeout runs on to at least 2.4 s, whatever the
    # fake's startup time adds.
    with_state(tmp_path, monkeypatch, read_delay=1.2)
    started = time.monotonic()
    with pytest.raises(PaneUnavailable, match="did not answer"):
        find_pane(9999, command=HERDR, timeout=2.0)
    elapsed = time.monotonic() - started
    reads = [call for call in herdr.calls() if call[:2] == ["pane", "process-info"]]
    assert len(reads) == 2  # the second read ran, and the deadline, not the list, ended it
    assert elapsed < 2.3


def test_a_title_with_glyphs_reads_whole(herdr, tmp_path, monkeypatch):
    glyphs = pane("w5:p1", agent="claude", session=SESSION, title="◑ role-01 ✳")
    with_state(tmp_path, monkeypatch, panes=[glyphs], agents=[])
    assert read_identity("w5:p1", command=HERDR).title == "◑ role-01 ✳"


def test_both_identity_reads_share_one_deadline(herdr, tmp_path, monkeypatch):
    # Each read answers in 0.8 s, inside a 1.2 s timeout on its own; the two together
    # outlast it. A shared deadline cuts the second read; separate ones let it finish.
    delayed = [[["pane", "get", "w1:p1"], 0.8], [["agent", "get", "w1:p1"], 0.8]]
    with_state(tmp_path, monkeypatch, delayed=delayed)
    started = time.monotonic()
    with pytest.raises(PaneUnavailable, match="did not answer"):
        read_identity("w1:p1", command=HERDR, timeout=1.2)
    assert time.monotonic() - started < 1.5
