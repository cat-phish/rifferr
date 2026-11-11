"""ChangeSet and JournalEntry: declared now, defined by C-CHANGESET / C-JOURNAL (M1).

They exist so ``LibraryBackend.apply`` can be part of the protocol from M0
(BACKEND-9). Don't add fields here outside those specs.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ChangeSet(BaseModel):
    """Proposed field changes. Shape defined by C-CHANGESET (M1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class JournalEntry(BaseModel):
    """Record of an applied ChangeSet with old values. Shape defined by C-JOURNAL (M1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
