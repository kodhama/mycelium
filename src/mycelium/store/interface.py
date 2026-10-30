"""The store's interface (KTD10): what every implementation offers."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Message:
    id: int
    role: str
    body: str
    created_at: str
    handled: bool


class CursorConflict(Exception):
    """The cursor was not where the caller expected; read it again and retry."""

    def __init__(self, role: str, name: str, expected: int, actual: int) -> None:
        super().__init__(f"cursor {role}/{name} is at {actual}, not {expected}")
        self.expected = expected
        self.actual = actual


class StoreRefused(Exception):
    """The store cannot be used safely: its schema is newer than this mycelium,
    or a symlink stands where mycelium's own file or folder should be."""


class Store(Protocol):
    def append(self, role: str, body: str) -> int:
        """Store one message for `role` and return its id, which only grows."""
        ...

    def read(self, role: str, after: int) -> list[Message]:
        """The role's messages with an id above `after`, oldest first."""
        ...

    def mark_handled(self, role: str, message_id: int) -> None:
        """Mark the role's message handled; marking it again changes nothing.

        Raises KeyError when the role has no message with that id."""
        ...

    def cursor(self, role: str, name: str) -> int:
        """Where the named cursor stands; 0 when nobody has moved it."""
        ...

    def move_cursor(self, role: str, name: str, expected: int, new: int) -> None:
        """Move the cursor from `expected` to `new`.

        Raises CursorConflict when the cursor is not at `expected`, and ValueError
        when `new` is below `expected`: a cursor never moves back."""
        ...

    def close(self) -> None: ...
