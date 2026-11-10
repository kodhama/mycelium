"""The SQLite store.

Durability bar: survive a process crash, not power loss
(docs/decisions/store-durability.md). WAL with synchronous=NORMAL keeps
every committed write through a killed process; fullfsync stays off. The
file rules it shares with the bindings live in mycelium.store.files.
"""

from datetime import UTC, datetime
from pathlib import Path

from mycelium.store import files
from mycelium.store.interface import CursorConflict, Message

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
        self._db = files.open_database(
            path, schema=_SCHEMA, version=SCHEMA_VERSION, busy_timeout=BUSY_TIMEOUT_SECONDS
        )

    @property
    def durability(self) -> dict[str, int | str]:
        """The settings that hold the crash-only bar, read from the open connection."""
        return {name: self._pragma(name) for name in ("journal_mode", "synchronous", "fullfsync")}

    def _pragma(self, name: str) -> int | str:
        return self._db.execute(f"PRAGMA {name}").fetchone()[0]

    def _write(self):
        return files.write(self._db)

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
