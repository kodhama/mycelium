"""The SQLite store.

Durability bar: survive a process crash, not power loss
(docs/decisions/store-durability.md). WAL with synchronous=NORMAL keeps
every committed write through a killed process; fullfsync stays off.
"""

import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from mycelium.store.interface import CursorConflict, Message, StoreRefused

SCHEMA_VERSION = 1

# Long enough for 20 concurrent posters to queue behind one writer.
BUSY_TIMEOUT_SECONDS = 30

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS messages_by_role ON messages (role, id)",
    """CREATE TABLE IF NOT EXISTS handled (
        message_id INTEGER PRIMARY KEY REFERENCES messages (id)
    )""",
    """CREATE TABLE IF NOT EXISTS cursors (
        role TEXT NOT NULL,
        name TEXT NOT NULL,
        position INTEGER NOT NULL,
        PRIMARY KEY (role, name)
    )""",
)


class SqliteStore:
    def __init__(self, path: Path) -> None:
        _make_private_dirs(path.parent)
        _check_store_files(path, tighten=False)
        # Refuse a newer file before anything here can write to it (but for a
        # hot rollback journal, which the read-write open must roll back first).
        _refuse_newer_schema(path)
        # isolation_level=None: no implicit transactions; each one below is explicit.
        # SQLite creates a missing file, so this process never holds a descriptor
        # of its own on it (see _check_store_files).
        self._db = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        try:
            # Before anything is written: a new file is empty until the schema.
            _check_store_files(path, tighten=True)
            self._create_schema()
            self._use_wal()
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.execute("PRAGMA fullfsync=OFF")
        except BaseException:
            self._db.close()
            raise

    @property
    def durability(self) -> dict[str, int | str]:
        """The settings that hold the crash-only bar, read from the open connection."""
        return {name: self._pragma(name) for name in ("journal_mode", "synchronous", "fullfsync")}

    def _create_schema(self) -> None:
        # A current file needs no lock, so an open never waits behind a writer.
        if self._checked_version() == SCHEMA_VERSION:
            return
        # Several first runs can race to create one file: BEGIN IMMEDIATE takes
        # the write lock before anything is created, and the version is read
        # again under it, so they create it in turn.
        with self._write():
            if self._checked_version() < SCHEMA_VERSION:
                for statement in _SCHEMA:
                    self._db.execute(statement)
                self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _use_wal(self) -> None:
        # journal_mode cannot change inside a transaction, and WAL persists in the
        # file. The switch needs the file to itself: it waits out the busy timeout
        # behind a reader but fails at once behind a writer, so an opener racing
        # another first run retries it here.
        deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
        while True:
            try:
                mode = self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                break
            except sqlite3.OperationalError as error:
                busy = error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                if not busy or time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        if mode != "wal":
            raise StoreRefused(f"the store cannot use WAL here (journal mode {mode})")

    def _checked_version(self) -> int:
        return _checked(self._pragma("user_version"))

    def _pragma(self, name: str) -> int | str:
        return self._db.execute(f"PRAGMA {name}").fetchone()[0]

    @contextmanager
    def _write(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            # SQLite may already have rolled back (a full disk, an I/O error).
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def append(self, role: str, body: str) -> int:
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        inserted = self._db.execute(
            "INSERT INTO messages (role, body, created_at) VALUES (?, ?, ?)", (role, body, now)
        )
        return inserted.lastrowid

    def read(self, role: str, after: int) -> list[Message]:
        rows = self._db.execute(
            "SELECT m.id, m.role, m.body, m.created_at, h.message_id IS NOT NULL"
            " FROM messages m LEFT JOIN handled h ON h.message_id = m.id"
            " WHERE m.role = ? AND m.id > ? ORDER BY m.id",
            (role, after),
        )
        return [Message(*row[:4], handled=bool(row[4])) for row in rows]

    def mark_handled(self, role: str, message_id: int) -> None:
        with self._write():
            found = self._db.execute(
                "SELECT 1 FROM messages WHERE id = ? AND role = ?", (message_id, role)
            ).fetchone()
            if found is None:
                raise KeyError(f"{role} has no message {message_id}")
            self._db.execute("INSERT OR IGNORE INTO handled VALUES (?)", (message_id,))

    def cursor(self, role: str, name: str) -> int:
        row = self._db.execute(
            "SELECT position FROM cursors WHERE role = ? AND name = ?", (role, name)
        ).fetchone()
        return 0 if row is None else row[0]

    def move_cursor(self, role: str, name: str, expected: int, new: int) -> None:
        if new < expected:
            raise ValueError(f"cursor {role}/{name} cannot move back from {expected} to {new}")
        with self._write():
            actual = self.cursor(role, name)
            if actual != expected:
                raise CursorConflict(role, name, expected, actual)
            self._db.execute(
                "INSERT INTO cursors (role, name, position) VALUES (?, ?, ?)"
                " ON CONFLICT (role, name) DO UPDATE SET position = excluded.position",
                (role, name, new),
            )

    def close(self) -> None:
        self._db.close()


def _make_private_dirs(folder: Path) -> None:
    """Create `folder` and any missing parents owner-only.

    The whole path must be canonical: a symlink anywhere in it, dangling or not,
    is refused before anything is created through it."""
    existing = folder
    while not os.path.lexists(existing):
        existing = existing.parent
    if existing.resolve() != existing or not existing.is_dir():
        raise StoreRefused(f"{folder} is not canonical (a symlink?); mycelium does not follow one")
    missing = [existing / part for part in _prefixes(folder.relative_to(existing))]
    for new in missing:
        try:
            new.mkdir(mode=0o700)
        except FileExistsError:
            continue  # another first run made it a moment ago; checked below
        new.chmod(0o700)  # mkdir's mode is narrowed by the umask, never widened
    if folder.resolve() != folder:
        raise StoreRefused(f"{folder} is not canonical (a symlink?); mycelium does not follow one")


def _prefixes(relative: Path) -> list[Path]:
    """`a/b/c` gives `a`, `a/b`, `a/b/c`."""
    return [Path(*relative.parts[: n + 1]) for n in range(len(relative.parts))]


def _checked(version: int) -> int:
    if version > SCHEMA_VERSION:
        raise StoreRefused(
            f"store schema {version} is newer than this mycelium reads"
            f" ({SCHEMA_VERSION}); update mycelium"
        )
    return version


def _refuse_newer_schema(path: Path) -> None:
    """Read the schema version over a read-only connection, which can neither
    checkpoint nor delete a WAL a crash left. It may still rebuild -shm, the
    index SQLite derives from the WAL, or add an empty -wal beside a clean file.

    A hot rollback journal, left by a crash before the switch to WAL, is the one
    thing it cannot read past. Such a file has no WAL to protect, so the check
    is left to the read-write open, which rolls the journal back first."""
    if not os.path.lexists(path):
        return
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_SECONDS)) as read_only:
        try:
            version = read_only.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK:
                return
            raise
    _checked(version)


def _store_files(path: Path) -> tuple[Path, Path, Path]:
    return path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def _check_store_files(path: Path, *, tighten: bool) -> None:
    """Refuse a store, -wal or -shm file that is a symlink, and with `tighten`
    make each one owner-only.

    Only by path, never through a descriptor: closing a descriptor on a database
    file drops every POSIX lock this process holds on it, a connection's
    included (sqlite.org/howtocorrupt.html, section 2.2), and another thread's
    connection may hold one. SQLite gives a new -wal or -shm file the database
    file's permissions."""
    for name in _store_files(path):
        try:
            mode = os.lstat(name).st_mode
        except FileNotFoundError:
            continue  # no -wal or -shm yet
        if stat.S_ISLNK(mode):
            raise StoreRefused(f"{name} is a symlink; mycelium does not follow one")
        if tighten and stat.S_IMODE(mode) != 0o600:
            with suppress(FileNotFoundError):  # a closing connection removed it
                os.chmod(name, 0o600)
