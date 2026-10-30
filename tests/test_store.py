import multiprocessing
import os
import signal
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from mycelium.store import (
    SCHEMA_VERSION,
    CursorConflict,
    StoreRefused,
    open_store,
)

# Spawned children import this module afresh, so the workers below sit at its
# top level and take only plain arguments.
CTX = multiprocessing.get_context("spawn")


@pytest.fixture
def store(tmp_path):
    opened = open_store(tmp_path / "store.sqlite3")
    yield opened
    opened.close()


def _run_together(target, args_per_worker, timeout):
    """Start one process per args tuple, release them together once every one is
    waiting, and wait for all. A worker still alive at the end is killed."""
    start = CTX.Event()
    ready = CTX.Semaphore(0)
    workers = [
        CTX.Process(target=_when_released, args=(ready, start, target, args), daemon=True)
        for args in args_per_worker
    ]
    try:
        for worker in workers:
            worker.start()
        for _ in workers:
            assert ready.acquire(timeout=timeout), "a worker never got ready"
        start.set()
        for worker in workers:
            worker.join(timeout)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join()


def _when_released(ready, start, target, args):
    ready.release()
    start.wait()
    target(*args)


def test_append_then_read_from_zero_returns_the_message(store):
    message_id = store.append("role-01", "hello")
    [message] = store.read("role-01", after=0)
    assert (message.id, message.role, message.body, message.handled) == (
        message_id,
        "role-01",
        "hello",
        False,
    )


def test_a_read_after_the_cursor_moves_returns_nothing(store):
    message_id = store.append("role-01", "hello")
    store.move_cursor("role-01", "digest", expected=0, new=message_id)
    assert store.cursor("role-01", "digest") == message_id
    assert store.read("role-01", after=store.cursor("role-01", "digest")) == []


def test_read_returns_only_the_roles_own_messages_in_order(store):
    first = store.append("role-01", "one")
    store.append("role-02", "other")
    second = store.append("role-01", "two")
    assert [m.id for m in store.read("role-01", after=0)] == [first, second]
    assert [m.body for m in store.read("role-01", after=first)] == ["two"]


def test_a_cursor_nobody_moved_reads_zero(store):
    assert store.cursor("role-01", "digest") == 0


def test_mark_handled_shows_on_read_and_is_idempotent(store):
    message_id = store.append("role-01", "hello")
    store.mark_handled("role-01", message_id)
    store.mark_handled("role-01", message_id)
    [message] = store.read("role-01", after=0)
    assert message.handled is True


def test_mark_handled_refuses_another_roles_message(store):
    message_id = store.append("role-01", "hello")
    with pytest.raises(KeyError):
        store.mark_handled("role-02", message_id)


def test_a_stale_expected_cursor_conflicts_and_a_retry_succeeds(tmp_path):
    path = tmp_path / "store.sqlite3"
    first, second = open_store(path), open_store(path)
    a = first.append("role-01", "one")
    b = first.append("role-01", "two")
    seen = second.cursor("role-01", "digest")
    first.move_cursor("role-01", "digest", expected=0, new=a)
    with pytest.raises(CursorConflict) as conflict:
        second.move_cursor("role-01", "digest", expected=seen, new=b)
    assert conflict.value.actual == a
    second.move_cursor("role-01", "digest", expected=conflict.value.actual, new=b)
    assert first.cursor("role-01", "digest") == b


def test_a_cursor_never_moves_backwards(store):
    a = store.append("role-01", "one")
    store.move_cursor("role-01", "digest", expected=0, new=a)
    with pytest.raises(ValueError):
        store.move_cursor("role-01", "digest", expected=a, new=a - 1)


def _move_once(path, results):
    store = open_store(path)
    try:
        store.move_cursor("role-01", "digest", expected=0, new=1)
        results.put("won")
    except CursorConflict:
        results.put("conflict")


def test_two_processes_moving_one_cursor_one_wins_and_one_conflicts(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).append("role-01", "one")
    results = CTX.Queue()
    _run_together(_move_once, [(path, results)] * 2, timeout=30)
    assert sorted(results.get(timeout=5) for _ in range(2)) == ["conflict", "won"]


def _open_and_append(path, n):
    open_store(path).append("role-01", f"from {n}")


def test_processes_creating_one_new_database_at_once_all_succeed(tmp_path):
    # U1 left this case unmeasured: several first runs racing to create the file.
    path = tmp_path / "fresh" / "store.sqlite3"
    _run_together(_open_and_append, [(path, n) for n in range(24)], timeout=60)
    bodies = sorted(m.body for m in open_store(path).read("role-01", after=0))
    assert bodies == sorted(f"from {n}" for n in range(24))
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def _post_many(path, poster, count):
    store = open_store(path)
    for i in range(count):
        store.append("role-01", f"{poster}:{i}")


def test_twenty_concurrent_posters_lose_and_duplicate_nothing(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    _run_together(_post_many, [(path, p, 10) for p in range(20)], timeout=60)
    bodies = [m.body for m in open_store(path).read("role-01", after=0)]
    assert sorted(bodies) == sorted(f"{p}:{i}" for p in range(20) for i in range(10))


def _post_forever(path, ready, acknowledged):
    store = open_store(path)
    ready.set()
    i = 0
    while True:
        store.append("role-01", f"body-{i}-" + "x" * 512)
        i += 1
        acknowledged.value = i


def test_a_killed_writer_loses_no_acknowledged_message(tmp_path):
    path = tmp_path / "store.sqlite3"
    ready = CTX.Event()
    acknowledged = CTX.Value("q", 0)
    writer = CTX.Process(target=_post_forever, args=(path, ready, acknowledged), daemon=True)
    writer.start()
    try:
        assert ready.wait(30)
        time.sleep(0.3)
        os.kill(writer.pid, signal.SIGKILL)
        writer.join(10)
        assert writer.exitcode == -signal.SIGKILL
    finally:
        if writer.is_alive():
            writer.kill()
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    messages = open_store(path).read("role-01", after=0)
    assert acknowledged.value > 0
    assert len(messages) >= acknowledged.value
    for n, message in enumerate(messages):
        assert message.body == f"body-{n}-" + "x" * 512


def test_the_bar_is_a_process_crash_wal_with_normal_sync(store, tmp_path):
    with closing(sqlite3.connect(tmp_path / "store.sqlite3")) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert store.durability == {"journal_mode": "wal", "synchronous": 1, "fullfsync": 0}


def test_opening_a_current_store_does_not_wait_for_a_writer(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        open_store(path).close()
        assert time.monotonic() - started < 1
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def _try_to_write(path, results):
    with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as other:
        try:
            other.execute("BEGIN IMMEDIATE")
            results.put("acquired")
        except sqlite3.OperationalError:
            results.put("blocked")


def test_opening_the_store_keeps_this_processs_write_lock(tmp_path):
    # Closing any descriptor on a database file drops every POSIX lock this
    # process holds on it (sqlite.org/howtocorrupt.html, section 2.2).
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        open_store(path).close()
        results = CTX.Queue()
        other = CTX.Process(target=_try_to_write, args=(path, results), daemon=True)
        other.start()
        other.join(30)
        assert results.get(timeout=5) == "blocked"
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _crash_after_writing(path, statements):
    # Exit without closing, so the WAL stays as a crash leaves it.
    raw = sqlite3.connect(path, isolation_level=None)
    for statement in statements:
        raw.execute(statement)
    os._exit(0)


def _crash_leaving_a_wal(path, *statements):
    crasher = CTX.Process(target=_crash_after_writing, args=(path, statements))
    crasher.start()
    crasher.join(30)
    assert crasher.exitcode == 0
    assert path.with_name(path.name + "-wal").stat().st_size > 0


def test_a_store_from_a_newer_mycelium_is_refused_and_left_as_it_was(tmp_path):
    # An older mycelium reading a newer file would misread it; it says so instead.
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    with closing(sqlite3.connect(path)) as raw:
        raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    before = _snapshot(path)
    with pytest.raises(StoreRefused, match="newer"):
        open_store(path)
    assert _snapshot(path) == before


def test_a_newer_store_with_a_crash_wal_is_refused_and_its_wal_kept(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    _crash_leaving_a_wal(
        path,
        f"PRAGMA user_version={SCHEMA_VERSION + 1}",
        "INSERT INTO messages (role, body, created_at) VALUES ('role-01', 'kept', 'now')",
    )
    before = _snapshot(path)
    with pytest.raises(StoreRefused, match="newer"):
        open_store(path)
    assert _snapshot(path) == before


def _snapshot(path):
    """The store's data, the WAL included, so a write that went only there shows
    too. It leaves out -shm, the index SQLite rebuilds from the WAL, and an empty
    -wal, which a read-only connection may add beside a clean file."""
    return {
        sibling.name: sibling.read_bytes()
        for sibling in path.parent.iterdir()
        if sibling.name.startswith(path.name)
        and not sibling.name.endswith("-shm")
        and sibling.stat().st_size > 0
    }


def test_new_folders_are_owner_only(tmp_path):
    path = tmp_path / "a" / "b" / "store.sqlite3"
    open_store(path).close()
    assert (tmp_path / "a").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "a" / "b").stat().st_mode & 0o777 == 0o700


def test_the_store_and_its_wal_files_are_owner_only(tmp_path):
    path = tmp_path / "store.sqlite3"
    store = open_store(path)
    store.append("role-01", "hello")
    for name in ("store.sqlite3", "store.sqlite3-wal", "store.sqlite3-shm"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name
    store.close()


def test_a_symlinked_store_file_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere.sqlite3"
    open_store(elsewhere).close()
    (tmp_path / "store.sqlite3").symlink_to(elsewhere)
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(tmp_path / "store.sqlite3")


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_a_symlinked_wal_or_shm_file_is_refused(tmp_path, suffix):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    target = tmp_path / "elsewhere"
    target.write_bytes(b"not sqlite")
    target.chmod(0o644)
    path.with_name(path.name + suffix).symlink_to(target)
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(path)
    assert target.read_bytes() == b"not sqlite"
    assert target.stat().st_mode & 0o777 == 0o644


def test_a_symlinked_store_folder_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "linked").symlink_to(tmp_path / "real")
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(tmp_path / "linked" / "store.sqlite3")


def test_a_store_still_in_rollback_mode_waits_for_a_writer_then_opens(tmp_path):
    # A first opener commits the schema before switching the file to WAL; an
    # opener arriving between the two must wait, not fail at once.
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    with closing(sqlite3.connect(path, isolation_level=None)) as raw:
        raw.execute("PRAGMA journal_mode=DELETE")
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.5, lambda: writer.execute("ROLLBACK"))
    release.start()
    try:
        store = open_store(path)
        assert store.durability["journal_mode"] == "wal"
        store.close()
    finally:
        release.join()
        writer.close()


def test_an_existing_loose_store_file_is_made_owner_only(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    path.chmod(0o644)
    store = open_store(path)
    store.append("role-01", "hello")
    for name in ("store.sqlite3", "store.sqlite3-wal", "store.sqlite3-shm"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name
    store.close()


def test_a_symlink_above_the_store_folder_is_refused(tmp_path):
    # $XDG_DATA_HOME/kodhama linked elsewhere, mycelium/ not made yet.
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "kodhama").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(tmp_path / "kodhama" / "mycelium" / "store.sqlite3")
    assert list((tmp_path / "elsewhere").iterdir()) == []


def test_a_dangling_symlink_folder_is_refused(tmp_path):
    (tmp_path / "gone").symlink_to(tmp_path / "missing")
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(tmp_path / "gone" / "store.sqlite3")


def test_a_folder_swapped_for_a_symlink_while_being_made_is_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def lose_the_race(folder, *args, **kwargs):
        folder.symlink_to(elsewhere)  # another process made it first, as a link
        raise FileExistsError(folder)

    monkeypatch.setattr(Path, "mkdir", lose_the_race)
    with pytest.raises(StoreRefused, match="symlink"):
        open_store(tmp_path / "made" / "store.sqlite3")
    assert list(elsewhere.iterdir()) == []


class _NoWal(sqlite3.Connection):
    """A connection whose switch to WAL leaves the file in rollback mode."""

    def execute(self, sql, *args):
        if sql == "PRAGMA journal_mode=WAL":
            return super().execute("SELECT 'delete'")
        return super().execute(sql, *args)


def test_a_store_that_cannot_use_wal_is_refused(tmp_path, monkeypatch):
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=_NoWal, **kw))
    with pytest.raises(StoreRefused, match="WAL"):
        open_store(tmp_path / "store.sqlite3")


def test_a_wal_switch_still_busy_at_the_deadline_raises(tmp_path, monkeypatch):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    with closing(sqlite3.connect(path, isolation_level=None)) as raw:
        raw.execute("PRAGMA journal_mode=DELETE")
    monkeypatch.setattr("mycelium.store.sqlite.BUSY_TIMEOUT_SECONDS", 0.2)
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")  # RESERVED: the switch fails at once, every try
    outcome = []

    def open_it():
        try:
            open_store(path).close()
            outcome.append("opened")
        except sqlite3.OperationalError as error:
            outcome.append(error.sqlite_errorcode)

    opener = threading.Thread(target=open_it, daemon=True)
    try:
        opener.start()
        opener.join(10)
        assert outcome == [sqlite3.SQLITE_BUSY]
    finally:
        writer.execute("ROLLBACK")
        writer.close()
        opener.join(10)


def test_a_loose_wal_and_shm_left_by_a_crash_are_made_owner_only(tmp_path):
    path = tmp_path / "store.sqlite3"
    open_store(path).close()
    _crash_leaving_a_wal(
        path, "INSERT INTO messages (role, body, created_at) VALUES ('role-01', 'kept', 'now')"
    )
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).chmod(0o644)
    store = open_store(path)
    for name in ("store.sqlite3", "store.sqlite3-wal", "store.sqlite3-shm"):
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o600, name
    assert [m.body for m in store.read("role-01", after=0)] == ["kept"]
    store.close()
