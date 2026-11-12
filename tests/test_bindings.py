"""Bindings: which session holds which role (R1-R4, R35, R37; KTD3-KTD6; hub plan U4)."""

import json
import os
import stat
from dataclasses import replace

import pytest

from mycelium import bindings, identity, lease
from mycelium.bindings import Context, Event, Refused
from mycelium.identity import Caller, Proc
from mycelium.index import refresh
from mycelium.launch import Launch
from mycelium.store import StoreRefused

BOOT = "boot-now"
NOW = 1_800_000_000.0


def claude(pid: int, session: str) -> Caller:
    return Caller("claude", pid, session, False, "walk", None)


def codex_tui(thread: str) -> Caller:
    return Caller("codex", None, thread, None, "walk", "a Codex TUI caller")


def codex_without_ps(thread: str) -> Caller:
    return Caller("codex", None, thread, None, "codex-thread-id", "ps denied")


def named(name: str) -> Launch:
    return Launch(name, None, {"environment": name}, "none")


class World:
    """A bindings store in a scratch folder, and the machine it binds on."""

    def __init__(self, tmp_path) -> None:
        self.homes = tmp_path / "homes"
        self.store = bindings.open_bindings(tmp_path / "state" / "bindings.sqlite3")
        self.path = tmp_path / "state" / "bindings.sqlite3"
        self.boot = BOOT
        self.now = NOW
        self.alive: set[int] = set()
        self.declared = {"ogham-worker", "ogham-lead"}

    def context(self) -> Context:
        return Context(
            boot=self.boot,
            now=self.now,
            declared=lambda name: name in self.declared,
            claim=lambda name: lease.read(lease.claim_path(self.homes, name)),
            alive=lambda pid, harness: pid in self.alive,
        )

    def bind(self, caller: Caller, launch="ogham-worker", source="startup"):
        if caller.pid is not None:
            self.alive.add(caller.pid)
        self.now += 1
        if isinstance(launch, str):
            launch = named(launch)
        return self.store.bind(Event(source, caller, launch), self.context())

    def adopt(self, caller: Caller, nonce: str):
        self.now += 1
        return self.store.adopt(caller, nonce, self.context())

    def resolve(self, caller: Caller):
        return self.store.resolve_caller(caller, self.context())

    def claim(self, name="ogham-worker", **over) -> None:
        data = {
            "state": "pending",
            "nonce": "n1",
            "resumer": "ogham-lead",
            "boot": self.boot,
            "expires": "2030-01-01T00:00:00.000Z",
            "pane": None,
            "session": None,
        } | over
        path = lease.claim_path(self.homes, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))


@pytest.fixture
def world(tmp_path):
    found = World(tmp_path)
    yield found
    found.store.close()


A = claude(1001, "s-a")
B = claude(2002, "s-b")


# Hub U4 scenario: AE2, newest wins with no lease, one notice, the older refused.


def test_without_a_lease_the_newer_binding_is_current_and_the_older_told_once(world):
    first = world.bind(A)
    assert first.binding.state == "current"
    assert first.binding.generation == 1
    second = world.bind(B)
    assert second.binding.state == "current"
    assert second.binding.generation == 2
    assert [(old.session_id, old.state) for old in second.superseded] == [("s-a", "superseded")]
    assert world.resolve(A).state == "superseded"
    again = world.bind(A, source="resume")  # A's hook re-runs: no second notice
    assert again.superseded == ()
    assert again.binding.state == "superseded"


def test_a_handle_from_the_older_binding_is_refused(world):
    world.bind(A)
    world.bind(B)
    with pytest.raises(Refused, match="superseded"):
        world.store.authorize(A, world.context())
    assert world.store.authorize(B, world.context()).session_id == "s-b"


# Hub U4 scenario: hooks installed twice.


def test_the_same_hook_twice_makes_one_binding_and_no_supersede(world):
    first = world.bind(A)
    second = world.bind(A)
    assert second.superseded == ()
    assert second.binding.state == "current"
    assert second.binding.generation == first.binding.generation
    assert second.binding.last_seen > first.binding.last_seen
    assert len(world.store.rows()) == 1


# Hub U4 scenario: with a lease, only the nonce wins (KTD6).


def test_under_a_lease_only_the_adopted_nonce_makes_a_session_current(world):
    world.claim(nonce="n1")
    a = world.bind(A)
    assert a.binding.state == "provisional"
    assert a.binding.generation is None
    # The manager takes over: a new claim with a new nonce, and B starts.
    world.claim(nonce="n2")
    b = world.bind(B)
    assert b.binding.state == "provisional"
    adopted = world.adopt(B, "n2")
    assert adopted.binding.state == "current"
    assert adopted.binding.nonce == "n2"
    assert adopted.binding.generation == 1
    assert [old.session_id for old in adopted.superseded] == ["s-a"]
    with pytest.raises(Refused, match="nonce does not match"):
        world.adopt(A, "n1")
    assert world.resolve(B).state == "current"


def test_adopting_again_with_the_same_nonce_changes_nothing(world):
    world.claim(nonce="n1")
    world.bind(A)
    first = world.adopt(A, "n1")
    again = world.adopt(A, "n1")
    assert again.binding.generation == first.binding.generation
    assert again.superseded == ()


def test_a_wrong_nonce_unbinds_a_provisional_session(world):
    world.claim(nonce="n1")
    world.bind(A)
    with pytest.raises(Refused, match="nonce does not match"):
        world.adopt(A, "n9")
    found = world.resolve(A)
    assert (found.state, found.reason) == ("unbound", "nonce mismatch: the lease's nonce differs")


def test_a_session_started_while_the_adopted_lease_runs_stays_provisional(world):
    world.claim(nonce="n1")
    world.bind(B)
    world.adopt(B, "n1")
    world.claim(state="live", expires=None, pane="w1:p1", session="s-b", nonce="n1")
    hand = world.bind(A)
    assert hand.binding.state == "provisional"
    assert hand.superseded == ()
    assert world.resolve(B).state == "current"


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"boot": "boot-before"}, "earlier boot"),
        ({"expires": "2020-01-01T00:00:00.000Z"}, "expired"),
        ({"state": "live", "expires": None, "pane": "w1:p1", "session": "s-other"}, "another"),
    ],
)
def test_adopt_refuses_a_claim_it_cannot_take(world, over, why):
    world.bind(A)
    world.claim(nonce="n1", **over)
    with pytest.raises(Refused, match=why):
        world.adopt(A, "n1")


def test_adopt_with_no_claim_or_no_binding_is_refused(world):
    with pytest.raises(Refused, match="no binding for this session yet"):
        world.adopt(A, "n1")
    world.bind(A)
    with pytest.raises(Refused, match="no lease"):
        world.adopt(A, "n1")


# Hub U4 scenario: a fork without the nonce.


def test_a_fork_without_the_nonce_binds_unbound_and_the_role_stays(world):
    world.bind(A)
    fork = world.bind(claude(3003, "s-fork"), source="fork")
    assert fork.binding.state == "unbound"
    assert "fork" in fork.binding.reason
    assert fork.superseded == ()
    assert world.resolve(A).state == "current"
    assert [row.session_id for row in world.store.live_bindings(world.context())] == ["s-a"]


def test_a_fork_that_adopts_the_nonce_becomes_current(world):
    world.claim(nonce="n1")
    fork = claude(3003, "s-fork")
    world.bind(fork, source="fork")
    assert world.adopt(fork, "n1").binding.state == "current"


# Hub U4 scenario: after a reboot, an old binding is dead.


def test_after_a_reboot_an_old_binding_reads_as_dead(world):
    world.bind(A)
    world.boot = "boot-after"
    context = world.context()
    (row,) = world.store.rows()
    assert bindings.liveness(row, context) == "dead"
    assert world.store.live_bindings(context) == []
    assert row.state == "current"  # dead is computed, never stored (KTD3)


def test_a_binding_whose_process_ended_reads_as_dead(world):
    world.bind(A)
    world.alive.clear()
    (row,) = world.store.rows()
    assert bindings.liveness(row, world.context()) == "dead"


def test_a_pid_less_binding_is_live_unverified_for_its_boot(world):
    world.bind(codex_tui("t-1"))
    (row,) = world.store.rows()
    assert bindings.liveness(row, world.context()) == "unverified"
    world.boot = "boot-after"
    assert bindings.liveness(row, world.context()) == "dead"


# Hub U4 scenario: AE8, no launch name.


def test_a_session_with_no_launch_name_is_unbound_and_told_so(world):
    found = world.bind(A, launch=Launch(None, "no launch name: none given", {}, "none"))
    assert found.binding.state == "unbound"
    assert found.binding.reason == "no launch name: none given"
    assert world.resolve(A).reason == "no launch name: none given"


def test_a_session_unbound_for_no_name_binds_once_a_name_appears(world):
    # herdr's record may not exist yet at the first SessionStart (lessons C2).
    world.bind(A, launch=Launch(None, "no launch name: none given", {}, "none"))
    later = world.bind(A, source="resume")
    assert later.binding.state == "current"
    assert later.binding.name == "ogham-worker"


def test_the_maintainer_is_never_bound(world):
    found = world.bind(A, launch=Launch(None, "@maintainer is reserved (R35)", {}, "none"))
    assert found.binding.state == "unbound"
    assert all(row.name != "@maintainer" for row in world.store.rows())
    with pytest.raises(ValueError, match="R35"):
        world.bind(B, launch=named("@maintainer"))


# Hub U4 scenario: AE11, undeclared.


def test_a_name_declared_nowhere_binds_as_undeclared(world):
    found = world.bind(A, launch="scratch-probe")
    assert found.binding.state == "current"
    assert found.binding.declared is False
    assert world.bind(B).binding.declared is True


# Hub U4 scenario: the CLI from a child of A resolves A; no harness ancestor is unbound.


def table(*procs: Proc):
    return lambda: {proc.pid: proc for proc in procs}


def test_a_child_of_session_a_resolves_as_a(world):
    world.bind(A)
    walked = identity.resolve(
        env={"CLAUDE_PID": "1001", "CLAUDE_CODE_SESSION_ID": "s-a"},
        pid=5000,
        table=table(Proc(5000, 4000, "python"), Proc(4000, 1001, "zsh"), Proc(1001, 1, "claude")),
    )
    found = world.resolve(walked)
    assert found.state == "current"
    assert found.binding.session_id == "s-a"


def test_a_caller_with_no_harness_ancestor_resolves_as_unbound(world):
    world.bind(A)
    walked = identity.resolve(env={}, pid=5000, table=table(Proc(5000, 1, "launchd-job")))
    found = world.resolve(walked)
    assert (found.state, found.binding) == ("unbound", None)
    assert found.reason == "no harness ancestor"


def test_a_session_with_no_binding_yet_resolves_as_unbound(world):
    found = world.resolve(A)
    assert (found.state, found.reason) == (
        "unbound",
        "no binding for this session yet: its SessionStart hook has not bound it",
    )


def test_a_claude_binding_for_another_pid_does_not_resolve(world):
    world.bind(A)
    found = world.resolve(replace(A, pid=9999))
    assert found.state == "unbound"
    assert "another process" in found.reason


def test_a_codex_caller_resolves_by_its_thread_id_alone(world):
    world.bind(Caller("codex", 7007, "t-exec", False, "walk", None))
    found = world.resolve(codex_without_ps("t-exec"))
    assert found.state == "current"


# Hub U4 scenario: a nested codex exec under a bound session.


def test_a_nested_codex_exec_binds_unbound_and_the_role_stays_current(world):
    world.bind(A)
    nested = Caller("codex", 8008, "t-nested", True, "walk", None)
    found = world.bind(nested)
    assert found.binding.state == "unbound"
    assert "nested" in found.binding.reason
    assert found.superseded == ()
    assert world.resolve(A).state == "current"


# Hub U4 scenario: after a crash, or a reboot, a hand or restored session becomes current.


def test_after_a_crash_a_live_claim_lets_a_hand_started_session_become_current(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.alive.discard(1001)  # A crashed; its claim stays live
    found = world.bind(B)
    assert found.binding.state == "current"
    assert [old.session_id for old in found.superseded] == ["s-a"]


def test_after_a_reboot_a_pane_restored_under_an_earlier_boot_claim_becomes_current(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.boot = "boot-after"
    world.alive.clear()
    # herdr restores the pane with `claude --resume s-a`: a new pid, the same session.
    restored = world.bind(claude(1500, "s-a"), source="resume")
    assert restored.binding.state == "current"
    assert restored.binding.boot == "boot-after"
    assert restored.binding.pid == 1500
    assert restored.binding.generation == 1  # a resume re-confirms its generation (KTD4)


# Hub U4 scenario: lessons G2, herdr's name differs at a later SessionStart.


@pytest.mark.parametrize("source", ["resume", "compact", "clear"])
def test_a_later_session_start_whose_herdr_name_now_differs_unbinds(world, source):
    world.bind(A)
    renamed = Launch(None, "launch names disagree", {"pane": "ogham-lead"}, "named")
    found = world.bind(A, launch=renamed, source=source)
    assert found.binding.state == "unbound"
    assert found.binding.reason == "launch names now disagree"
    assert world.store.live_bindings(world.context()) == []


def test_a_later_session_start_that_still_agrees_keeps_the_binding(world):
    first = world.bind(A)
    found = world.bind(A, launch=Launch("ogham-worker", None, {"pane": "ogham-worker"}, "named"))
    assert found.binding.state == "current"
    assert found.binding.generation == first.binding.generation


# Hub U4 scenario: lessons G3, a /resume onto another session in the same pid.


def test_a_resume_onto_another_session_in_the_same_pid_supersedes_the_older_once(world):
    world.bind(A)
    found = world.bind(claude(1001, "s-a2"), source="resume")
    assert found.binding.state == "current"
    assert found.binding.generation == 2
    assert [old.session_id for old in found.superseded] == ["s-a"]
    assert [row.session_id for row in world.store.live_bindings(world.context())] == ["s-a2"]


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_a_resume_or_compact_of_the_same_session_keeps_its_generation(world, source):
    first = world.bind(A)
    again = world.bind(A, source=source)
    assert again.binding.generation == first.binding.generation
    assert again.superseded == ()


def test_a_clear_in_the_same_process_starts_a_new_generation(world):
    first = world.bind(A)
    cleared = world.bind(claude(1001, "s-a-cleared"), source="clear")
    assert cleared.binding.state == "current"
    assert cleared.binding.generation == first.binding.generation + 1
    assert [old.session_id for old in cleared.superseded] == ["s-a"]


def test_a_clear_in_the_process_that_holds_the_lease_keeps_the_lease(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    cleared = world.bind(claude(1001, "s-a-cleared"), source="clear")
    assert cleared.binding.state == "current"
    assert cleared.binding.nonce == "n1"
    assert cleared.binding.generation == 2


def test_a_clear_then_a_resume_back_in_the_same_pid_makes_the_resumed_session_current(world):
    # Lessons G3: one process /resumes back onto the session it left.
    a, b = claude(1001, "s-a"), claude(1001, "s-b")
    world.bind(a)
    world.bind(b, source="clear")
    back = world.bind(a, source="resume")
    assert back.binding.state == "current"
    assert back.binding.generation == 3
    assert [old.session_id for old in back.superseded] == ["s-b"]
    assert world.resolve(a).state == "current"
    assert world.resolve(b).state == "superseded"


# KTD4: a resume in a new process (herdr restores `claude --resume <sid>`, same boot).


def test_a_lease_holder_resumed_in_a_new_pid_keeps_its_lease_and_a_later_clear_inherits_it(
    world,
):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.alive.discard(1001)
    restored = world.bind(claude(1500, "s-a"), source="resume")
    assert (restored.binding.state, restored.binding.generation) == ("current", 1)
    assert (restored.binding.nonce, restored.binding.pid) == ("n1", 1500)
    assert restored.superseded == ()
    cleared = world.bind(claude(1500, "s-a2"), source="clear")
    assert (cleared.binding.state, cleared.binding.nonce) == ("current", "n1")
    assert cleared.binding.generation == 2
    live = world.store.live_bindings(world.context())
    assert [row.session_id for row in live] == ["s-a2"]


def test_a_superseded_session_resumed_in_a_new_pid_stays_superseded(world):
    world.bind(A)
    world.bind(B)
    resumed = world.bind(claude(1500, "s-a"), source="resume")
    assert resumed.binding.state == "superseded"
    assert resumed.superseded == ()
    assert world.resolve(B).state == "current"


def test_a_fork_resumed_in_a_new_pid_stays_unbound(world):
    world.bind(A)
    world.bind(claude(3003, "s-fork"), source="fork")
    resumed = world.bind(claude(3500, "s-fork"), source="resume")
    assert resumed.binding.state == "unbound"
    assert resumed.binding.reason == bindings.FORK
    assert resumed.superseded == ()
    assert world.resolve(A).state == "current"


def test_a_session_unbound_by_g2_is_decided_again_once_its_names_agree(world):
    world.bind(A)
    renamed = Launch(None, "launch names disagree", {"pane": "ogham-lead"}, "named")
    world.bind(A, launch=renamed, source="resume")
    again = world.bind(A, source="resume")
    assert again.binding.state == "current"
    assert again.binding.name == "ogham-worker"


def test_a_provisional_binding_whose_claim_expired_becomes_current_at_its_next_resume(world):
    world.claim(nonce="n1")
    first = world.bind(A)
    assert first.binding.state == "provisional"
    twice = world.bind(A)  # the hook installed twice: nothing changes
    assert (twice.binding.state, twice.superseded) == ("provisional", ())
    world.claim(nonce="n1", expires="2020-01-01T00:00:00.000Z")
    resumed = world.bind(A, source="resume")
    assert resumed.binding.state == "current"
    assert resumed.binding.generation == 1


def test_a_session_decided_under_another_name_does_not_carry_the_old_names_generation(world):
    world.bind(A)  # ogham-worker, generation 1
    world.claim(name="ogham-lead", nonce="n1")
    renamed = world.bind(claude(1500, "s-a"), launch="ogham-lead")
    assert (renamed.binding.name, renamed.binding.state) == ("ogham-lead", "provisional")
    assert renamed.binding.generation is None


# The lead's ruling on a re-decided session (GRO-50 lead note, 2026-11-10; KTD4).

RENAMED = Launch(None, "launch names disagree", {"pane": "ogham-lead"}, "named")


@pytest.mark.parametrize("disagreeing", [1, 2])
def test_a_g2_unbound_session_whose_names_agree_again_keeps_its_generation(world, disagreeing):
    world.bind(B)  # generation 1
    first = world.bind(A)  # generation 2
    for _ in range(disagreeing):  # G2, then the hook re-run while the names still disagree
        assert world.bind(A, launch=RENAMED, source="resume").binding.state == "unbound"
    again = world.bind(A, source="resume")
    assert (again.binding.state, again.binding.generation) == ("current", first.binding.generation)
    assert again.superseded == ()


def test_an_expired_provisional_session_nobody_adopted_takes_the_names_next_generation(world):
    world.bind(B)  # generation 1
    world.alive.discard(2002)
    world.claim(nonce="n1")
    first = world.bind(A)
    assert (first.binding.state, first.binding.generation) == ("provisional", None)
    world.claim(nonce="n1", expires="2020-01-01T00:00:00.000Z")
    later = world.bind(A, source="resume")
    assert (later.binding.state, later.binding.generation) == ("current", 2)
    assert [old.session_id for old in later.superseded] == ["s-b"]


def test_a_g2_unbound_session_whose_name_another_held_since_stays_out_until_it_adopts(world):
    world.bind(A)  # generation 1
    world.bind(A, launch=RENAMED, source="resume")
    world.bind(B)  # generation 2; A is unbound, so nothing of A's is superseded
    again = world.bind(A, source="resume")
    assert again.binding.state == "unbound"
    assert "another session held ogham-worker since" in again.binding.reason
    assert again.superseded == ()
    assert world.resolve(B).state == "current"
    world.alive.discard(2002)  # the holder dies: re-running the hook still takes nothing back
    still = world.bind(A, source="resume")
    assert (still.binding.state, still.superseded) == ("unbound", ())
    world.claim(nonce="n1")
    adopted = world.adopt(A, "n1")
    assert (adopted.binding.state, adopted.binding.generation) == ("current", 3)
    assert [old.session_id for old in adopted.superseded] == ["s-b"]


@pytest.mark.parametrize("ended", ["holder died", "lease expired"])
@pytest.mark.parametrize("mismatched", [False, True])
def test_a_provisional_session_whose_lease_another_adopted_stays_out(world, ended, mismatched):
    world.claim(nonce="n1")
    assert world.bind(A).binding.state == "provisional"
    if mismatched:  # a wrong nonce unbinds it before the lease is adopted
        with pytest.raises(Refused, match="nonce does not match"):
            world.adopt(A, "n9")
    world.bind(B)
    assert world.adopt(B, "n1").binding.generation == 1
    if ended == "holder died":
        world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-b")
        world.alive.discard(2002)
    else:
        world.claim(nonce="n2", expires="2020-01-01T00:00:00.000Z")
    later = world.bind(A, source="resume")
    assert later.binding.state in ("superseded", "unbound")
    assert later.binding.generation is None
    assert later.superseded == ()
    if later.binding.state == "unbound":
        assert "another session held ogham-worker since" in later.binding.reason
    assert world.resolve(B).state == "current"


def test_a_session_whose_herdr_name_changed_binds_at_the_new_names_next_generation(world):
    world.bind(B, launch="ogham-lead")  # ogham-lead generation 1
    world.bind(claude(3003, "s-c"), launch="ogham-lead")  # ogham-lead generation 2
    world.bind(A)  # ogham-worker generation 1
    world.bind(A, launch=RENAMED, source="resume")
    moved = world.bind(A, launch="ogham-lead", source="resume")
    found = moved.binding
    assert (found.name, found.state, found.generation) == ("ogham-lead", "current", 3)
    assert [old.session_id for old in moved.superseded] == ["s-c"]


# touch: a session's prompt refreshes last_seen (lead ruling, GRO-50; U8's hook calls it).


def test_touch_refreshes_last_seen_and_changes_nothing_else(world):
    first = world.bind(A).binding
    world.now += 100
    touched = world.store.touch(A, world.context())
    assert touched.state == "current"
    (row,) = world.store.rows()
    assert row == replace(first, last_seen=world.now, bound_seen=world.now)  # seen bound
    assert touched.binding == row


def test_touch_without_a_binding_of_this_boot_does_nothing_and_says_so(world):
    found = world.store.touch(A, world.context())
    assert (found.state, found.binding, found.reason) == ("unbound", None, bindings.NO_BINDING_YET)
    assert world.store.rows() == []
    world.bind(A)
    (before,) = world.store.rows()
    world.boot = "boot-after"
    world.now += 100
    found = world.store.touch(A, world.context())
    assert (found.state, found.binding) == ("unbound", None)
    assert "earlier boot" in found.reason
    assert world.store.rows() == [before]


# The nested=None rule (lead ruling, GRO-50).


def test_a_caller_with_unknown_nesting_never_supersedes_a_walk_verified_binding(world):
    world.bind(A)
    found = world.bind(codex_without_ps("t-1"))
    assert found.binding.state == "unbound"
    assert "verified" in found.binding.reason
    assert found.superseded == ()
    assert world.resolve(A).state == "current"


def test_a_caller_with_unknown_nesting_may_supersede_a_pid_less_binding(world):
    world.bind(codex_tui("t-old"))
    found = world.bind(codex_tui("t-new"))  # a Codex TUI restarted in place
    assert found.binding.state == "current"
    assert [old.session_id for old in found.superseded] == ["t-old"]


def test_a_caller_with_unknown_nesting_may_supersede_a_dead_binding(world):
    world.bind(A)
    world.alive.clear()
    found = world.bind(codex_without_ps("t-1"))
    assert found.binding.state == "current"


def test_each_binding_records_its_evidence(world):
    world.bind(A)
    world.bind(codex_without_ps("t-1"), launch="ogham-lead")
    evidence = {row.session_id: (row.pid, row.evidence) for row in world.store.rows()}
    assert evidence == {"s-a": (1001, "walk"), "t-1": (None, "codex-thread-id")}


def test_may_supersede_is_one_decision():
    walked = bindings.Binding(
        "claude", "s-a", "ogham-worker", True, 1001, "walk", BOOT, 1, "current", None, None, 0, 0
    )
    unknown = codex_without_ps("t-1")
    assert not bindings.may_supersede(unknown, walked, "live")
    assert not bindings.may_supersede(unknown, walked, "unverified")
    assert bindings.may_supersede(unknown, walked, "dead")
    assert bindings.may_supersede(unknown, replace(walked, pid=None), "unverified")
    assert bindings.may_supersede(claude(2002, "s-b"), walked, "live")


# What binds nothing.


def test_a_caller_the_walk_could_not_resolve_binds_nothing(world):
    found = world.bind(Caller(None, None, None, None, "walk", "no harness ancestor"))
    assert found.binding is None
    assert found.reason == "no harness ancestor"
    assert world.store.rows() == []


def test_a_caller_with_no_session_id_binds_nothing(world):
    found = world.bind(Caller("claude", 1001, None, False, "walk", "CLAUDE_PID ..."))
    assert found.binding is None
    assert world.store.rows() == []


def test_without_a_boot_id_nothing_binds(world):
    world.boot = None
    found = world.bind(A)
    assert found.binding is None
    assert "boot id" in found.reason
    assert world.store.rows() == []


def test_an_unknown_hook_source_is_refused(world):
    with pytest.raises(ValueError, match="source"):
        world.bind(A, source="restart")


# The file, under the store's rules.


def test_the_bindings_file_is_owner_only(world):
    world.bind(A)
    assert stat.S_IMODE(os.stat(world.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(world.path.parent).st_mode) == 0o700


def test_a_symlinked_bindings_file_is_refused(tmp_path):
    real = tmp_path / "elsewhere.sqlite3"
    real.touch()
    folder = tmp_path / "state"
    folder.mkdir()
    (folder / "bindings.sqlite3").symlink_to(real)
    with pytest.raises(StoreRefused, match="symlink"):
        bindings.open_bindings(folder / "bindings.sqlite3")


def test_a_bindings_file_from_a_newer_mycelium_is_refused(tmp_path):
    import sqlite3

    path = tmp_path / "bindings.sqlite3"
    with sqlite3.connect(path) as raw:
        raw.execute(f"PRAGMA user_version={bindings.SCHEMA_VERSION + 1}")
    with pytest.raises(StoreRefused, match="newer"):
        bindings.open_bindings(path)


def test_a_suffixed_checkout_reads_its_claim_under_the_declared_name_at_its_home(tree):
    # Grove ruling (GRO-50): the claim sits at the cluster's home, under the name
    # cluster.toml declares; the effective name's suffix comes from instance.toml.
    found = tree("org")
    index = refresh(found.paths, read=found.read).index
    home = found.home("hq/mq/repos/engine")
    assert index.slice("ex-engine-lead-427").declared_name == "ex-engine-lead"
    declared = lease.claim_path(home, "ex-engine-lead")
    declared.parent.mkdir(parents=True)
    claim = {"state": "live", "nonce": "n-declared", "resumer": "lead", "boot": BOOT}
    declared.write_text(json.dumps(claim))
    effective = lease.claim_path(home, "ex-engine-lead-427")
    effective.parent.mkdir(parents=True)
    effective.write_text(json.dumps({**claim, "nonce": "n-effective"}))

    assert bindings.claim_reader(index)("ex-engine-lead-427").nonce == "n-declared"


# The lead's overrule (ii) (GRO-50 lead note, 2026-11-10): the stay-out rule holds only
# for a holder from the current boot; after a reboot, a returning session is a fresh start.


def test_after_a_reboot_a_superseded_session_restored_alone_takes_the_next_generation(world):
    world.bind(A)  # X: generation 1
    world.bind(B)  # Y supersedes X: generation 2
    world.boot = "boot-after"
    world.alive.clear()
    restored = world.bind(claude(1500, "s-a"), source="resume")  # only X's pane comes back
    assert (restored.binding.state, restored.binding.generation) == ("current", 3)
    assert [old.session_id for old in restored.superseded] == ["s-b"]


def test_after_a_reboot_a_session_kept_out_by_an_earlier_boot_holder_takes_the_next_generation(
    world,
):
    world.bind(A)  # generation 1
    world.bind(A, launch=RENAMED, source="resume")  # A unbound by G2
    world.bind(B)  # generation 2: within this boot, A stays out
    assert world.bind(A, source="resume").binding.state == "unbound"
    world.boot = "boot-after"
    world.alive.clear()
    restored = world.bind(claude(1500, "s-a"), source="resume")
    assert (restored.binding.state, restored.binding.generation) == ("current", 3)


@pytest.mark.parametrize("holder", ["started after the reboot", "restored after the reboot"])
def test_after_a_reboot_a_holder_of_the_current_boot_still_keeps_a_session_out(world, holder):
    world.bind(A)  # generation 1
    world.bind(A, launch=RENAMED, source="resume")  # A unbound by G2
    if holder == "restored after the reboot":
        world.bind(B)  # generation 2, before the reboot
    world.boot = "boot-after"
    world.alive.clear()
    if holder == "started after the reboot":
        held = world.bind(claude(3003, "s-c"))
    else:
        held = world.bind(claude(2500, "s-b"), source="resume")  # keeps generation 2
    assert (held.binding.state, held.binding.generation) == ("current", 2)
    back = world.bind(claude(1500, "s-a"), source="resume")
    assert back.binding.state == "unbound"
    assert "another session held ogham-worker since" in back.binding.reason
    assert back.superseded == ()


# A re-decided session keeps the lease nonce it adopted (floor re-review, GRO-50).


@pytest.mark.parametrize("claim_state", ["pending", "live"])
def test_a_session_decided_again_keeps_the_lease_it_adopted(world, claim_state):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    if claim_state == "live":
        world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.bind(A, launch=RENAMED, source="resume")  # G2 unbinds it
    again = world.bind(A, source="resume")  # its names agree again
    assert (again.binding.state, again.binding.nonce) == ("current", "n1")
    assert again.binding.generation == 1
    cleared = world.bind(claude(1001, "s-a2"), source="clear")
    assert (cleared.binding.state, cleared.binding.nonce) == ("current", "n1")


# adopt (floor re-review, GRO-50).


def test_adopting_supersedes_another_session_of_the_callers_own_process(world):
    world.bind(A)  # ogham-worker
    world.bind(claude(1001, "s-a2"), launch="ogham-lead", source="clear")
    world.claim(nonce="n1")
    adopted = world.adopt(A, "n1")  # a /resume back to s-a whose hook has not run yet
    assert adopted.binding.state == "current"
    assert [old.session_id for old in adopted.superseded] == ["s-a2"]
    assert world.resolve(claude(1001, "s-a2")).state == "superseded"


@pytest.mark.parametrize(
    "over, why",
    [
        ({"boot": "boot-before"}, "earlier boot"),
        ({"expires": "2020-01-01T00:00:00.000Z"}, "expired"),
    ],
)
def test_a_claim_adopt_cannot_take_refuses_before_any_nonce_check(world, over, why):
    world.claim(nonce="n1")
    assert world.bind(A).binding.state == "provisional"
    world.claim(nonce="n2", **over)
    with pytest.raises(Refused, match=why):
        world.adopt(A, "n1")
    assert world.resolve(A).state == "provisional"


# bound_at follows the name (floor re-review, GRO-50).


def test_a_session_decided_under_a_new_name_is_bound_from_then(world):
    first = world.bind(A).binding
    world.claim(name="ogham-lead", nonce="n1")
    renamed = world.bind(claude(1500, "s-a"), launch="ogham-lead").binding
    assert renamed.name == "ogham-lead"
    assert renamed.bound_at == world.now > first.bound_at
    world.claim(name="ogham-lead", nonce="n1", expires="2020-01-01T00:00:00.000Z")
    again = world.bind(claude(1500, "s-a"), launch="ogham-lead", source="resume").binding
    assert (again.state, again.bound_at) == ("current", renamed.bound_at)


# Floor re-review at 9814746 (GRO-50).

NO_NAME = Launch(None, "no launch name", {}, "none")


@pytest.mark.parametrize("claim_state", ["pending", "live"])
def test_a_session_keeps_its_lease_through_a_start_with_no_launch_name(world, claim_state):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    if claim_state == "live":
        world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.bind(A, launch=RENAMED, source="resume")  # G2 unbinds it
    world.bind(A, launch=NO_NAME, source="compact")  # still no agreed name
    again = world.bind(A, source="resume")
    assert (again.binding.state, again.binding.nonce) == ("current", "n1")


def test_a_holder_that_keeps_its_generation_still_keeps_a_session_out(world):
    world.bind(B)  # generation 1
    world.bind(B, launch=RENAMED, source="resume")  # B unbound by G2
    world.claim(nonce="n1")
    assert world.bind(A).binding.state == "provisional"
    with pytest.raises(Refused, match="nonce does not match"):
        world.adopt(A, "n9")  # A leaves the name's holders
    world.claim(nonce="n1", expires="2020-01-01T00:00:00.000Z")
    back = world.bind(B, source="resume")  # B holds the name again, at generation 1
    assert (back.binding.state, back.binding.generation) == ("current", 1)
    again = world.bind(A, source="resume")
    assert again.binding.state == "unbound"
    assert "another session held ogham-worker since" in again.binding.reason
    assert world.resolve(B).state == "current"


# ce-doc-review at 17e551a (GRO-50).


@pytest.mark.parametrize("first", ["Y", "X"])
def test_after_a_reboot_the_first_restored_holder_keeps_the_others_out(world, first):
    world.bind(A)  # X: generation 1
    world.bind(B)  # Y supersedes X: generation 2
    world.boot = "boot-after"
    world.alive.clear()
    x, y = claude(1500, "s-a"), claude(2500, "s-b")
    order = [y, x] if first == "Y" else [x, y]
    held = world.bind(order[0], source="resume")
    assert held.binding.state == "current"
    late = world.bind(order[1], source="resume")
    assert late.binding.state == "unbound"
    assert "another session held ogham-worker since" in late.binding.reason
    assert late.superseded == ()
    assert world.resolve(order[0]).state == "current"


def test_a_running_lease_holder_unbound_by_g2_keeps_the_lease_in_force(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.bind(A, launch=RENAMED, source="resume")  # G2: A still runs, unbound
    found = world.bind(B)  # a hand-started session
    assert found.binding.state == "provisional"


# ce-code-review of the whole branch at 9814746 (GRO-50).


def test_a_clear_after_a_g2_unbind_keeps_the_running_lease(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.bind(A, launch=RENAMED, source="resume")  # G2 unbinds it
    cleared = world.bind(claude(1001, "s-a2"), source="clear")
    assert (cleared.binding.state, cleared.binding.nonce) == ("current", "n1")


def test_a_session_adopts_a_live_lease_another_session_of_its_process_holds(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-a")
    world.bind(claude(1001, "s-a2"), source="clear")
    adopted = world.adopt(claude(1001, "s-a2"), "n1")
    assert (adopted.binding.state, adopted.binding.nonce) == ("current", "n1")
    other = claude(3003, "s-c")  # a session of another process
    assert world.bind(other).binding.state == "provisional"
    with pytest.raises(Refused, match="another session holds"):
        world.adopt(other, "n1")


# The last review at e153df4 (GRO-50).


def test_a_superseded_session_never_keeps_a_lease_in_force(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.bind(B)
    world.adopt(B, "n1")  # B adopts the same claim: A is superseded, its process alive
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-b")
    world.alive.discard(2002)  # B exits
    found = world.bind(claude(3003, "s-c"))
    assert found.binding.state == "current"


def test_after_a_reboot_a_restored_fork_stays_out_once_the_name_is_held(world):
    world.bind(A)  # generation 1
    world.bind(claude(3003, "s-fork"), source="fork")  # unbound: never held the name
    world.boot = "boot-after"
    world.alive.clear()
    assert world.bind(claude(1500, "s-a"), source="resume").binding.state == "current"
    fork = world.bind(claude(3500, "s-fork"), source="resume")
    assert fork.binding.state == "unbound"
    assert fork.superseded == ()
    assert world.resolve(claude(1500, "s-a")).state == "current"


def test_a_session_kept_out_gives_up_the_lease_another_adopted(world):
    world.claim(nonce="n1")
    world.bind(A)
    world.adopt(A, "n1")
    world.bind(A, launch=RENAMED, source="resume")  # G2 unbinds A, which keeps n1
    world.bind(B)
    world.adopt(B, "n1")  # B holds the name and the lease
    world.claim(nonce="n1", state="live", expires=None, pane="w1:p1", session="s-b")
    kept_out = world.bind(A, source="resume")
    assert (kept_out.binding.state, kept_out.binding.nonce) == ("unbound", None)
    cleared = world.bind(claude(1001, "s-a2"), source="clear")
    assert cleared.binding.state == "provisional"
    assert world.resolve(B).state == "current"
