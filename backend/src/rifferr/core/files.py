"""Folder cleanup models (S-FILES-CLEANUP): listings, busy folders, cleanup decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CleanupClass = Literal["delete", "keep", "in_library", "busy", "left", "empty"]
EntryKind = Literal["file", "dir", "link"]
CleanupState = Literal["pending", "deleted", "kept", "cancelled"]
CleanupChoice = Literal["delete", "keep"]


class CleanupEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    """Relative to the cleaned folder, with ``/``; ``"."`` is the folder itself."""
    kind: EntryKind
    cls: CleanupClass
    size: int | None = None
    mtime_ns: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class BusyFolder:
    """A folder something else still needs (FILES-CLEANUP-7)."""

    path: Path
    reason: str


class CleanupOptions(BaseModel):
    """What the worker needs to clean again after the decision (FILES-CLEANUP-14)."""

    model_config = ConfigDict(frozen=True)

    delete: list[str] = Field(default_factory=list[str])
    keep: list[str] = Field(default_factory=list[str])
    empty_dirs: bool = True
    remove_root: bool = False
    trash: str | None = None
    allow_audio: bool = False


class CleanupDecision(BaseModel):
    """A listing waiting for Delete or Keep in the review queue (FILES-CLEANUP-14)."""

    id: str
    profile: str
    run_id: str
    step_id: str
    path: str
    state: CleanupState = "pending"
    entries: list[CleanupEntry]
    truncated: int = 0
    totals: dict[str, int] = Field(default_factory=dict[str, int])
    options: CleanupOptions
    created_at: datetime
    deadline: datetime | None = None
    on_timeout: CleanupChoice = "keep"
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None
    result: dict[str, int] | None = None

    @property
    def lane_key(self) -> str:
        """The lane a pass without albums waits in (LANES-16)."""
        return f"pending:cleanup:{self.id}"
