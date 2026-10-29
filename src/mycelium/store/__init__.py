"""The store behind one interface (KTD10): append, read from a cursor, mark
handled, and move a cursor atomically. Adapters and the index never touch it
directly (R27). U1 chose SQLite (docs/decisions/store-durability.md).
"""

from pathlib import Path

from mycelium.store.interface import CursorConflict, Message, Store, StoreRefused
from mycelium.store.sqlite import SCHEMA_VERSION, SqliteStore


def open_store(path: Path) -> Store:
    """Open the store at `path`, creating its folders and file (owner-only) and its
    schema if needed. Raises StoreRefused rather than misread or follow a symlink."""
    return SqliteStore(path)


__all__ = ["SCHEMA_VERSION", "CursorConflict", "Message", "Store", "StoreRefused", "open_store"]
