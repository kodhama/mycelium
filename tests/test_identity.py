import inspect
import os
import sys

import pytest

from mycelium.identity import (
    HARNESSES,
    PS_COMMAND,
    UNSUPPORTED,
    Caller,
    Proc,
    ProcessTableUnavailable,
    parse_ps,
    process_table,
    resolve,
)

SELF = 900  # the mycelium process the walk starts from
DAEMON = "/home/maintainer/.codex/packages/app-server-daemon/releases/0.160.0/bin/codex"
CLAUDE_SESSION = "00000000-0000-0000-0000-00000000c1a0"
CODEX_THREAD = "00000000-0000-0000-0000-00000000c0de"


def chain(*comms: str, start: int = SELF) -> dict[int, Proc]:
    """A process table where `start` is a child of comms[0], a child of comms[1], and so on.

    The process at comms[i] gets pid `start - 100 * (i + 1)`; the last one's parent is pid 1.
    """
    table = {start: Proc(start, start - 100, "mycelium")}
    for index, comm in enumerate(comms):
        pid = start - 100 * (index + 1)
        parent = pid - 100 if index + 1 < len(comms) else 1
        table[pid] = Proc(pid, parent, comm)
    table[1] = Proc(1, 0, "/sbin/launchd")
    return table


def walk(table, env=None, pid=SELF) -> Caller:
    return resolve(env={} if env is None else env, pid=pid, table=lambda: table)


def denied():
    raise ProcessTableUnavailable("ps exited 1: operation not permitted")


# Scenario 1: a child of a claude process, through a shell layer.
def test_a_child_of_claude_resolves_to_that_claude_with_its_own_session():
    table = chain("/bin/bash", "claude", "-zsh")
    caller = walk(table, {"CLAUDE_PID": "700", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert caller == Caller(
        harness="claude",
        pid=700,
        session_id=CLAUDE_SESSION,
        nested=False,
        source="walk",
        reason=None,
    )
    assert caller.resolved


def test_a_claude_whose_own_session_id_is_missing_says_so():
    caller = walk(chain("/bin/bash", "claude"), {"CLAUDE_PID": "700"})

    assert (caller.harness, caller.pid, caller.session_id) == ("claude", 700, None)
    assert "CLAUDE_CODE_SESSION_ID" in caller.reason


# Scenario 2: CLAUDE_PID was inherited from another claude.
def test_an_inherited_claude_pid_leaves_the_session_unknown():
    table = chain("/bin/bash", "claude", "-zsh")
    caller = walk(table, {"CLAUDE_PID": "4242", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert (caller.harness, caller.pid, caller.session_id) == ("claude", 700, None)
    assert caller.resolved
    assert "CLAUDE_PID" in caller.reason


def test_no_claude_pid_at_all_leaves_the_session_unknown():
    caller = walk(chain("claude"), {"CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert (caller.harness, caller.pid, caller.session_id) == ("claude", 800, None)


# Scenario 3: no harness above the caller (KTD5).
def test_no_harness_ancestor_is_unbound():
    caller = walk(chain("/bin/bash", "-zsh"), {"CODEX_THREAD_ID": CODEX_THREAD})

    assert caller == Caller(
        harness=None,
        pid=None,
        session_id=None,
        nested=None,
        source="walk",
        reason="no harness ancestor",
    )
    assert not caller.resolved


def test_the_caller_itself_is_not_its_own_ancestor():
    table = chain("/bin/bash")
    table[SELF] = Proc(SELF, 800, "claude")

    assert walk(table).reason == "no harness ancestor"


def test_a_cycle_in_the_parent_links_ends_the_walk():
    table = {
        SELF: Proc(SELF, 800, "mycelium"),
        800: Proc(800, 700, "sh"),
        700: Proc(700, 800, "sh"),
    }

    assert walk(table).reason == "no harness ancestor"


def test_a_missing_parent_ends_the_walk():
    table = {SELF: Proc(SELF, 800, "mycelium"), 700: Proc(700, 1, "claude")}

    assert walk(table).reason == "no harness ancestor"


def test_a_caller_missing_from_the_table_is_unbound():
    caller = walk(chain("claude"), pid=12345)

    assert caller.harness is None
    assert caller.reason.startswith("no harness ancestor")
    assert "12345" in caller.reason


# Scenario 4: a Claude started inside a bound Claude.
def test_a_claude_inside_a_claude_is_the_nearer_one_and_nested():
    table = chain("/bin/bash", "claude", "/bin/zsh", "claude", "-zsh")
    caller = walk(table, {"CLAUDE_PID": "700", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert (caller.harness, caller.pid, caller.session_id) == ("claude", 700, CLAUDE_SESSION)
    assert caller.nested is True


def test_the_outer_claudes_inherited_variables_do_not_reach_the_nested_one():
    table = chain("/bin/bash", "claude", "/bin/zsh", "claude", "-zsh")
    caller = walk(table, {"CLAUDE_PID": "500", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert (caller.pid, caller.session_id, caller.nested) == (700, None, True)


# Scenario 5: a codex exec inside a Claude session.
def test_a_codex_exec_inside_claude_is_codex_with_its_own_pid_and_nested():
    table = chain("/bin/bash", "/usr/local/bin/codex", "/bin/zsh", "claude", "-zsh")
    env = {
        "CLAUDE_PID": "500",
        "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION,
        "CODEX_THREAD_ID": CODEX_THREAD,
    }
    caller = walk(table, env)

    assert caller == Caller(
        harness="codex",
        pid=700,
        session_id=CODEX_THREAD,
        nested=True,
        source="walk",
        reason=None,
    )


def test_a_codex_exec_without_a_thread_id_stays_codex_with_the_session_unknown():
    caller = walk(chain("/bin/bash", "codex"))

    assert (caller.harness, caller.pid, caller.session_id, caller.nested) == (
        "codex",
        700,
        None,
        False,
    )
    assert "CODEX_THREAD_ID" in caller.reason


def test_a_codex_exec_under_a_codex_tui_daemon_counts_as_nested():
    table = chain("/bin/bash", "codex", "/bin/bash", DAEMON, "codex", "-zsh")
    caller = walk(table, {"CODEX_THREAD_ID": CODEX_THREAD})

    assert (caller.harness, caller.pid, caller.nested) == ("codex", 700, True)


# Scenario 6: a Codex TUI caller; its tools run under the shared app-server daemon.
def test_a_codex_tui_caller_is_codex_by_thread_id_with_pid_and_nesting_unknown():
    table = chain("/bin/bash", DAEMON, "codex", "-zsh")
    caller = walk(table, {"CODEX_THREAD_ID": CODEX_THREAD})

    assert (caller.harness, caller.pid, caller.session_id, caller.nested, caller.source) == (
        "codex",
        None,
        CODEX_THREAD,
        None,
        "walk",
    )
    assert caller.resolved
    assert caller.reason


def test_a_codex_tui_caller_under_a_claude_is_still_undecidable():
    table = chain("/bin/bash", DAEMON, "codex", "/bin/zsh", "claude")
    caller = walk(table, {"CODEX_THREAD_ID": CODEX_THREAD})

    assert (caller.harness, caller.pid, caller.nested) == ("codex", None, None)


def test_a_codex_tui_caller_without_a_thread_id_is_unbound():
    caller = walk(chain("/bin/bash", DAEMON, "codex"))

    assert caller.harness is None
    assert not caller.resolved
    assert "CODEX_THREAD_ID" in caller.reason


# Scenario 7: no process table.
def test_without_a_process_table_the_codex_thread_id_identifies_codex():
    caller = resolve(env={"CODEX_THREAD_ID": CODEX_THREAD}, pid=SELF, table=denied)

    assert (caller.harness, caller.pid, caller.session_id, caller.nested, caller.source) == (
        "codex",
        None,
        CODEX_THREAD,
        None,
        "codex-thread-id",
    )
    assert "could not run" in caller.reason


def test_without_a_process_table_or_a_thread_id_the_caller_is_unbound():
    env = {"CLAUDE_PID": "700", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION}
    caller = resolve(env=env, pid=SELF, table=denied)

    assert caller.harness is None
    assert caller.source == "none"
    assert "could not run" in caller.reason
    assert "operation not permitted" in caller.reason


@pytest.mark.parametrize(
    "marker",
    [
        {"CLAUDE_PID": "700"},
        {"CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION},
        {"CLAUDE_PID": "700", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION},
    ],
)
def test_without_a_process_table_a_claude_marker_beside_a_thread_id_is_nested_and_unbound(marker):
    # Each harness inherits the other's variables (probes-u2), so with no walk a Claude
    # marker beside CODEX_THREAD_ID means one runs inside the other, whichever way round.
    caller = resolve(env={"CODEX_THREAD_ID": CODEX_THREAD, **marker}, pid=SELF, table=denied)

    assert (caller.harness, caller.pid, caller.session_id, caller.nested, caller.source) == (
        None,
        None,
        None,
        True,
        "none",
    )
    assert not caller.resolved
    assert "could not run" in caller.reason
    assert "nested" in caller.reason


# Scenario 8: Droid or opencode nearest.
@pytest.mark.parametrize("name", UNSUPPORTED)
def test_an_unsupported_harness_nearest_is_unbound_and_named(name):
    table = chain("/bin/bash", f"/usr/local/bin/{name}", "/bin/zsh", "claude")
    caller = walk(table, {"CLAUDE_PID": "500", "CLAUDE_CODE_SESSION_ID": CLAUDE_SESSION})

    assert caller.harness is None
    assert name in caller.reason
    assert "Claude and Codex only" in caller.reason


def test_an_unsupported_harness_above_a_claude_makes_it_nested():
    table = chain("/bin/bash", "claude", "/bin/zsh", "opencode")

    assert walk(table).nested is True


# Scenario 9: parsing.
def test_parse_ps_keeps_spaces_in_comm_and_skips_malformed_lines():
    text = (
        "  PID  PPID COMM\n"
        "\n"
        "    1     0 /sbin/launchd\n"
        "  610     1 /Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Claude Helper\n"
        "  620   610 /Applications/Claude.app/Contents/MacOS/Claude  \n"
        "  630   620\n"
        "  abc   620 claude\n"
        "  700   620 claude\n"
    )

    table = parse_ps(text)

    assert sorted(table) == [1, 610, 620, 700]
    assert table[610].name == "Claude Helper"
    assert table[620] == Proc(620, 610, "/Applications/Claude.app/Contents/MacOS/Claude")
    assert table[620].name == "Claude"
    assert table[700].name == "claude"


def test_the_desktop_app_is_not_a_harness():
    table = parse_ps(
        "  900   800 mycelium\n"
        "  800   700 /bin/bash\n"
        "  700   610 /Applications/Claude.app/Contents/MacOS/Claude\n"
        "  610     1 /Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Claude Helper\n"
    )

    assert walk(table).reason == "no harness ancestor"


def test_the_harness_names_are_the_ones_u4_knows():
    assert HARNESSES == ("claude", "codex")
    assert UNSUPPORTED == ("droid", "opencode")
    assert PS_COMMAND == ("/bin/ps", "-axww", "-o", "pid=,ppid=,comm=")


def test_resolve_takes_no_caller_name():
    # KTD5: the caller is never identified from an argument naming it.
    assert list(inspect.signature(resolve).parameters) == ["env", "pid", "table"]


# Scenario 10: the real ps.
def test_the_real_process_table_holds_this_test_with_its_parent():
    table = process_table()

    assert table[os.getpid()].ppid == os.getppid()


# process_table failures, through a substituted argv.
def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_a_missing_ps_is_unavailable(tmp_path):
    with pytest.raises(ProcessTableUnavailable, match="not found"):
        process_table([str(tmp_path / "no-such-ps")])


def test_a_failing_ps_is_unavailable_with_what_it_said():
    code = "import sys; print('ps: operation not permitted', file=sys.stderr); sys.exit(1)"
    with pytest.raises(ProcessTableUnavailable, match="exited 1: ps: operation not permitted"):
        process_table(python(code))


def test_a_slow_ps_is_unavailable():
    with pytest.raises(ProcessTableUnavailable, match="0.2s"):
        process_table(python("import time; time.sleep(5)"), timeout=0.2)


def test_a_ps_that_lists_nothing_is_unavailable():
    with pytest.raises(ProcessTableUnavailable, match="no processes"):
        process_table(python("print('garbage')"))


def test_process_table_parses_what_ps_prints():
    table = process_table(python("print('  700   1 claude')"))

    assert table == {700: Proc(700, 1, "claude")}


# Review fixes (GRO-49).
def test_ps_is_run_by_absolute_path_so_path_cannot_hide_or_replace_it():
    # A caller that empties PATH must not push the walk onto the thread-id fallback.
    assert PS_COMMAND[0] == "/bin/ps"


def test_a_codex_exec_a_tui_tool_shell_replaced_sits_under_the_daemon_and_is_nested():
    # `zsh -lc 'codex exec ...'` replaces the shell with the exec, so the exec's
    # parent is the daemon itself. A codex parent therefore does not mark a daemon;
    # only the daemon's path does.
    table = chain("/bin/bash", "/opt/tools/codex", DAEMON, "codex", "-zsh")
    caller = walk(table, {"CODEX_THREAD_ID": CODEX_THREAD})

    assert (caller.harness, caller.pid, caller.nested) == ("codex", 700, True)


def test_a_daemon_execd_without_its_path_reads_as_a_codex_exec_documented_limit():
    # Pins the decision record's known limit until plan U11 confirms the daemon's
    # exec name on a live Codex: the walk then takes the shared pid, nested true.
    table = chain("/bin/bash", "codex", "codex", "-zsh")
    caller = walk(table, {"CODEX_THREAD_ID": CODEX_THREAD})

    assert (caller.harness, caller.pid, caller.nested, caller.reason) == ("codex", 700, True, None)
