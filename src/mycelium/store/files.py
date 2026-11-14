"""Owner-only SQLite files, as every mycelium database opens them.

The message store and the bindings share these rules (docs/decisions/store-durability.md,
docs/decisions/bindings.md):

- the folders are made owner-only (`0700`), and a symlink anywhere in the
  path is refused before anything is created through it;
- the file and its `-wal` and `-shm` files are owner-only (`0600`), and a
  symlink in place of any of them is refused;
- a file written by a newer mycelium is refused before anything is written;
- WAL with synchronous=NORMAL and fullfsync off: a committed write survives a
  killed process, not power loss.
"""

import os
import sqlite3
import stat
import time
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager, suppress
from pathlib import Path

from mycelium.store.interface import StoreRefused


def open_database(
    path: Path, *, schema: Sequence[str], version: int, busy_timeout: float
) -> sqlite3.Connection:
    """Open the database at `path`, creating its folders, file and schema if needed.

    Raises StoreRefused for a symlink found as it opens, a file newer than
    `version`, or a file that cannot use WAL."""
    make_private_dirs(path.parent)
    check_files(path, tighten=False)
    # Refuse a newer file before anything here can write to it (but for a
    # hot rollback journal, which the read-write open must roll back first).
    refuse_newer_schema(path, version, busy_timeout)
    # Before connecting: SQLite opens a file it cannot write read-only.
    check_files(path, tighten=True)
    # isolation_level=None: no implicit transactions; each one is explicit.
    # SQLite creates a missing file, so this process never holds a descriptor
    # of its own on it (see check_files).
    db = sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)
    try:
        # Again for a file SQLite just created, before anything is written:
        # it is empty until the schema.
        check_files(path, tighten=True)
        _create_schema(db, schema, version)
        _use_wal(db, busy_timeout)
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA fullfsync=OFF")
    except BaseException:
        db.close()
        raise
    return db


@contextmanager
def write(db: sqlite3.Connection) -> Iterator[None]:
    """One write transaction, holding the write lock from its start."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        # SQLite may already have rolled back (a full disk, an I/O error).
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    db.execute("COMMIT")


def _create_schema(db: sqlite3.Connection, schema: Sequence[str], version: int) -> None:
    # A current file needs no lock, so an open never waits behind a writer.
    if checked(_user_version(db), version) == version:
        return
    # Several first runs can race to create one file: BEGIN IMMEDIATE takes
    # the write lock before anything is created, and the version is read
    # again under it, so they create it in turn.
    with write(db):
        if checked(_user_version(db), version) < version:
            for statement in schema:
                db.execute(statement)
            db.execute(f"PRAGMA user_version={version}")


def _use_wal(db: sqlite3.Connection, busy_timeout: float) -> None:
    # journal_mode cannot change inside a transaction, and WAL persists in the
    # file. The switch needs the file to itself: it waits out the busy timeout
    # behind a reader but fails at once behind a writer, so an opener racing
    # another first run retries it here.
    deadline = time.monotonic() + busy_timeout
    while True:
        try:
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            break
        except sqlite3.OperationalError as error:
            busy = error.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if not busy or time.monotonic() > deadline:
                raise
            time.sleep(0.01)
    if mode != "wal":
        raise StoreRefused(f"the store cannot use WAL here (journal mode {mode})")


def _user_version(db: sqlite3.Connection) -> int:
    return db.execute("PRAGMA user_version").fetchone()[0]


def exists(path: Path) -> bool:
    """Whether anything, a symlink included, is at `path`.

    Unlike `os.path.lexists`, a path that can't be looked at, such as one under
    a folder that can't be searched, raises its OSError instead of reading as
    absent. A path under a file reads as absent, as nothing can be there."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def make_private_dirs(folder: Path) -> None:
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


def checked(found: int, supported: int) -> int:
    if found > supported:
        raise StoreRefused(
            f"store schema {found} is newer than this mycelium reads ({supported}); update mycelium"
        )
    return found


def refuse_newer_schema(path: Path, supported: int, busy_timeout: float) -> None:
    """Read the schema version over a read-only connection, which can neither
    checkpoint nor delete a WAL a crash left. It may still rebuild -shm, the
    index SQLite derives from the WAL, or add an empty -wal beside a clean file.

    A hot rollback journal, left by a crash before the switch to WAL, is the one
    thing it cannot read past. Such a file has no WAL to protect, so the check
    is left to the read-write open, which rolls the journal back first."""
    if not os.path.lexists(path):
        return
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=busy_timeout)) as read_only:
        try:
            found = read_only.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_READONLY_ROLLBACK:
                return
            raise
    checked(found, supported)


def _database_files(path: Path) -> tuple[Path, Path, Path]:
    return path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")


def check_files(path: Path, *, tighten: bool) -> None:
    """Refuse a database, -wal or -shm file that is a symlink, and with `tighten`
    make each one owner-only.

    Only by path, never through a descriptor: closing a descriptor on a database
    file drops every POSIX lock this process holds on it, a connection's
    included (sqlite.org/howtocorrupt.html, section 2.2), and another thread's
    connection may hold one. SQLite gives a new -wal or -shm file the database
    file's permissions."""
    for name in _database_files(path):
        try:
            mode = os.lstat(name).st_mode
        except FileNotFoundError:
            continue  # no -wal or -shm yet
        if stat.S_ISLNK(mode):
            raise StoreRefused(f"{name} is a symlink; mycelium does not follow one")
        if tighten and stat.S_IMODE(mode) != 0o600:
            with suppress(FileNotFoundError):  # a closing connection removed it
                os.chmod(name, 0o600)
