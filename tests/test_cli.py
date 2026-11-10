import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from mycelium import bindings, boot, cli, clusters, identity, lease
from mycelium import paths as mycelium_paths
from mycelium.cli import main
from mycelium.index import refresh
from mycelium.launch import Launch


def test_version_flag_prints_the_installed_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out == f"mycelium {version('mycelium')}\n"


def test_python_dash_m_runs_the_same_command():
    result = subprocess.run(
        [sys.executable, "-m", "mycelium", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == f"mycelium {version('mycelium')}\n"


def test_paths_prints_each_folder(tmp_path, monkeypatch, capsys):
    tmp_path = Path(os.path.realpath(tmp_path))
    for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["paths"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        f"state {tmp_path}/.local/state/kodhama/mycelium",
        f"data {tmp_path}/.local/share/kodhama/mycelium",
        f"cache {tmp_path}/.cache/kodhama/mycelium",
        f"clusters {tmp_path}/.local/state/kodhama/clusters",
    ]
    assert list(tmp_path.iterdir()) == []


# whoami, adopt and directory (hub plan U4; lead rulings, GRO-50).

CLAUDE_PID = 4242
BOOT = "boot-now"


class Machine:
    """This test process as a child of a fake `claude`, with XDG folders of its own."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.monkeypatch = monkeypatch
        xdg = Path(os.path.realpath(tmp_path)) / "machine-xdg"
        xdg.mkdir()
        for name, folder in (("STATE", "state"), ("DATA", "data"), ("CACHE", "cache")):
            monkeypatch.setenv(f"XDG_{name}_HOME", str(xdg / folder))
        self.paths = mycelium_paths.resolve()
        procs = {
            os.getpid(): identity.Proc(os.getpid(), 4300, "python"),
            4300: identity.Proc(4300, CLAUDE_PID, "zsh"),
            CLAUDE_PID: identity.Proc(CLAUDE_PID, 1, "claude"),
        }
        monkeypatch.setattr(identity, "process_table", lambda *a, **kw: procs)
        monkeypatch.setattr(boot, "boot_id", lambda *a, **kw: BOOT)
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        monkeypatch.delenv("KODHAMA_AGENT", raising=False)
        self.session("s-1")
        self.claims: dict[str, lease.Claim] = {}
        self.claim_for = lambda name: self.claims.get(name, lease.Claim("none"))
        self.now = 1_800_000_000.0

    def session(self, session_id: str) -> None:
        self.monkeypatch.setenv("CLAUDE_PID", str(CLAUDE_PID))
        self.monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)

    def context(self) -> bindings.Context:
        return bindings.Context(
            boot=BOOT,
            now=self.now,
            declared=lambda name: True,
            claim=self.claim_for,
            alive=lambda pid, harness: True,
        )

    def bind(self, session_id, name="ogham-worker", source="startup", pid=CLAUDE_PID):
        self.now += 1
        caller = identity.Caller("claude", pid, session_id, False, "walk", None)
        launch = Launch(name, None, {"environment": name}, "none")
        store = bindings.open_bindings(self.paths.bindings)
        try:
            return store.bind(bindings.Event(source, caller, launch), self.context())
        finally:
            store.close()


@pytest.fixture
def machine(tmp_path, monkeypatch):
    return Machine(tmp_path, monkeypatch)


def run(capsys, *argv):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_whoami_prints_the_name_alone_then_the_generation(machine, capsys):
    machine.bind("s-1")
    assert run(capsys, "whoami") == (0, "ogham-worker\ngeneration 1\n", "")


def test_whoami_json_prints_the_full_record(machine, capsys):
    machine.bind("s-1")
    code, out, err = run(capsys, "whoami", "--json")
    assert (code, err) == (0, "")
    record = json.loads(out)
    assert record["name"] == "ogham-worker"
    assert record["generation"] == 1
    assert record["state"] == "current"
    assert record["session_id"] == "s-1"
    assert record["pid"] == CLAUDE_PID


def test_whoami_for_a_superseded_caller_exits_1_with_the_reason_on_stderr(machine, capsys):
    machine.bind("s-1")
    machine.bind("s-2", pid=5555)
    for args in (["whoami"], ["whoami", "--json"]):
        code, out, err = run(capsys, *args)
        assert (code, out) == (1, "")
        assert "superseded" in err


def test_whoami_for_a_provisional_caller_exits_1(machine, capsys):
    machine.claims["ogham-worker"] = lease.Claim("pending", nonce="n1", boot=BOOT, expires=2e9)
    machine.bind("s-1")
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (1, "")
    assert "provisional" in err
    assert "adopt" in err


def test_whoami_for_an_unbound_caller_exits_1(machine, capsys):
    machine.bind("s-1", source="fork")
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (1, "")
    assert "unbound" in err
    assert "fork" in err


def test_whoami_with_no_harness_ancestor_exits_1_and_creates_nothing(machine, capsys):
    alone = {os.getpid(): identity.Proc(os.getpid(), 1, "x")}
    machine.monkeypatch.setattr(identity, "process_table", lambda *a, **kw: alone)
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (1, "")
    assert "no harness ancestor" in err
    assert not machine.paths.state.exists()


def test_whoami_before_any_binding_exits_1_and_creates_nothing(machine, capsys):
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (1, "")
    assert bindings.NO_BINDING_YET in err
    assert not machine.paths.state.exists()


def test_whoami_with_an_unreadable_bindings_store_exits_3(machine, capsys, tmp_path):
    machine.paths.state.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.sqlite3"
    elsewhere.touch()
    machine.paths.bindings.symlink_to(elsewhere)
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (3, "")
    assert "symlink" in err


def test_whoami_with_an_unreadable_state_folder_exits_3(machine, capsys):
    machine.paths.state.parent.mkdir(parents=True)
    machine.paths.state.write_text("a file where the folder should be")
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (3, "")
    assert err


def test_whoami_without_a_boot_id_exits_3(machine, capsys):
    machine.bind("s-1")
    machine.monkeypatch.setattr(boot, "boot_id", lambda *a, **kw: None)
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (3, "")
    assert "boot id" in err


@pytest.mark.parametrize("argv", [["whoami", "--bogus"], ["no-such-command"], ["adopt"]])
def test_a_usage_error_exits_2(machine, capsys, argv):
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, "")
    assert "usage" in err


def test_a_usage_error_exits_2_from_the_command_line():
    result = subprocess.run(
        [sys.executable, "-m", "mycelium", "whoami", "--bogus"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_whoami_across_an_in_place_clear(machine, capsys):
    # The lead's pin (GRO-50): same claude pid, same pane, same name, a new session id.
    machine.bind("s-1")
    assert run(capsys, "whoami") == (0, "ogham-worker\ngeneration 1\n", "")
    machine.session("s-2")  # /clear: CLAUDE_CODE_SESSION_ID changes, the pid stays
    code, out, err = run(capsys, "whoami")
    assert (code, out) == (1, "")
    assert err == f"mycelium whoami: unbound: {bindings.NO_BINDING_YET}\n"
    machine.bind("s-2", source="clear")  # the clear's SessionStart hook
    assert run(capsys, "whoami") == (0, "ogham-worker\ngeneration 2\n", "")
    assert run(capsys, "whoami") == (0, "ogham-worker\ngeneration 2\n", "")


# adopt and directory read the index; the tree fixture gives them clusters.


@pytest.fixture
def org(machine, tree, monkeypatch):
    found = tree("org")
    monkeypatch.setattr(cli, "read_clusters", found.read)
    for name in ("STATE", "DATA", "CACHE"):
        folder = getattr(found.paths, name.lower()).parent.parent
        monkeypatch.setenv(f"XDG_{name}_HOME", str(folder))
    machine.paths = mycelium_paths.resolve()
    machine.claim_for = bindings.claim_reader(refresh(found.paths, read=found.read).index)
    return found


def grove_claim(found, name: str, nonce: str) -> None:
    slice_ = refresh(found.paths, read=found.read).index.slice(name)
    path = lease.claim_path(slice_.home, slice_.declared_name)
    path.parent.mkdir(parents=True)
    claim = {
        "state": "pending",
        "nonce": nonce,
        "resumer": "root-lead",
        "boot": BOOT,
        "expires": "2099-01-01T00:00:00.000Z",
        "pane": None,
        "session": None,
    }
    path.write_text(json.dumps(claim))


def test_adopt_with_the_claims_nonce_makes_the_caller_current(machine, org, capsys):
    grove_claim(org, "mq-pm", "n1")
    machine.bind("s-1", name="mq-pm")
    assert run(capsys, "whoami")[0] == 1  # provisional under the lease
    assert run(capsys, "adopt", "n1") == (0, "mq-pm\ngeneration 1\n", "")
    assert run(capsys, "whoami") == (0, "mq-pm\ngeneration 1\n", "")


def test_adopt_with_a_wrong_nonce_exits_1(machine, org, capsys):
    grove_claim(org, "mq-pm", "n1")
    machine.bind("s-1", name="mq-pm")
    code, out, err = run(capsys, "adopt", "n-wrong")
    assert (code, out) == (1, "")
    assert "nonce does not match" in err


def test_adopt_with_the_helper_unavailable_exits_3(machine, capsys, monkeypatch):
    machine.bind("s-1")

    def unavailable():
        raise clusters.HelperUnavailable("kodhama-cluster is not installed")

    monkeypatch.setattr(cli, "read_clusters", unavailable)
    code, out, err = run(capsys, "adopt", "n1")
    assert (code, out) == (3, "")
    assert "kodhama-cluster" in err


def test_directory_prints_every_name_as_json(machine, org, capsys):
    machine.bind("s-1", name="mq-pm")
    machine.bind("s-2", name="scratch-probe", pid=5555)
    code, out, err = run(capsys, "directory")
    assert (code, err) == (0, "")
    listed = json.loads(out)
    assert set(listed) == {"names"}
    rows = {row["name"]: row for row in listed["names"]}
    assert set(refresh(org.paths, read=org.read).index.names()) <= set(rows)
    for row in rows.values():
        assert set(row) == {
            "name",
            "declared",
            "deliverability",
            "harness",
            "generation",
            "last_seen",
            "knock",
        }
    assert rows["mq-pm"]["deliverability"] == "live"
    assert rows["mq-pm"]["knock"] == {"can": True, "how": "sendmessage-hint"}
    assert rows["scratch-probe"]["declared"] is False
    assert rows["mq-roadmap-assistant"]["deliverability"] == "queue-only"


def test_directory_with_the_helper_unavailable_exits_3(machine, capsys, monkeypatch):
    def unavailable():
        raise clusters.HelperUnavailable("kodhama-cluster is not installed")

    monkeypatch.setattr(cli, "read_clusters", unavailable)
    code, out, err = run(capsys, "directory")
    assert (code, out) == (3, "")
    assert "kodhama-cluster" in err
