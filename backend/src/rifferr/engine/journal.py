"""The journal: write-ahead records of every apply (C-JOURNAL).

The engine appends a ``pending`` entry before calling ``LibraryBackend.apply``
and completes it afterwards (JOURNAL-1, -2). The app DB implementation lives in
``rifferr.appdb``; engine tests use ``InMemoryJournal`` (JOURNAL-8).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from rifferr.core.changeset import ApplyResult, JournalEntry


class JournalError(RuntimeError):
    """A journal operation that breaks its rules (e.g. completing an entry twice)."""


class Journal(Protocol):
    """Injected into the engine. ``begin`` must be durable before it returns."""

    def begin(self, entry: JournalEntry) -> None:
        """Append ``entry`` in state ``pending``."""
        ...

    def complete(self, entry_id: str, result: ApplyResult) -> JournalEntry:
        """``applied`` with effects and write failures, or ``failed`` with conflicts."""
        ...

    def fail(self, entry_id: str, error: str) -> JournalEntry:
        """``failed`` because apply raised before changing anything."""
        ...

    def pending(self, profile: str) -> list[JournalEntry]: ...

    def get(self, entry_id: str) -> JournalEntry | None: ...

    def for_run(self, run_id: str) -> list[JournalEntry]:
        """Entries of ``run_id``, oldest first."""
        ...

    def mark_undone(self, entry_id: str, by: str) -> None: ...


def completed(entry: JournalEntry, result: ApplyResult, now: datetime) -> JournalEntry:
    """The entry after ``apply`` returned (JOURNAL-2)."""
    _check_pending(entry)
    if result.applied:
        return entry.model_copy(
            update={
                "state": "applied",
                "effects": result.effects,
                "write_failures": result.write_failures,
                "completed_at": now,
            }
        )
    return entry.model_copy(
        update={"state": "failed", "conflicts": result.conflicts, "completed_at": now}
    )


def failed(entry: JournalEntry, error: str, now: datetime) -> JournalEntry:
    _check_pending(entry)
    return entry.model_copy(update={"state": "failed", "error": error, "completed_at": now})


def undone(entry: JournalEntry, by: str) -> JournalEntry:
    if entry.state != "applied":
        raise JournalError(f"journal entry {entry.id} is {entry.state}, not applied")
    if entry.undone_by is not None:
        raise JournalError(f"journal entry {entry.id} is already undone by {entry.undone_by}")
    return entry.model_copy(update={"undone_by": by})


def _check_pending(entry: JournalEntry) -> None:
    if entry.state != "pending":
        raise JournalError(f"journal entry {entry.id} is already {entry.state}")


class InMemoryJournal:
    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self.entries: dict[str, JournalEntry] = {}

    def begin(self, entry: JournalEntry) -> None:
        if entry.id in self.entries:
            raise JournalError(f"journal entry {entry.id} exists")
        _check_pending(entry)
        self.entries[entry.id] = entry

    def complete(self, entry_id: str, result: ApplyResult) -> JournalEntry:
        entry = completed(self._get(entry_id), result, self._now())
        self.entries[entry_id] = entry
        return entry

    def fail(self, entry_id: str, error: str) -> JournalEntry:
        entry = failed(self._get(entry_id), error, self._now())
        self.entries[entry_id] = entry
        return entry

    def pending(self, profile: str) -> list[JournalEntry]:
        return [e for e in self.entries.values() if e.profile == profile and e.state == "pending"]

    def get(self, entry_id: str) -> JournalEntry | None:
        return self.entries.get(entry_id)

    def for_run(self, run_id: str) -> list[JournalEntry]:
        return sorted(
            (e for e in self.entries.values() if e.run_id == run_id), key=lambda e: e.created_at
        )

    def mark_undone(self, entry_id: str, by: str) -> None:
        self.entries[entry_id] = undone(self._get(entry_id), by)

    def _get(self, entry_id: str) -> JournalEntry:
        entry = self.entries.get(entry_id)
        if entry is None:
            raise JournalError(f"no journal entry {entry_id}")
        return entry
