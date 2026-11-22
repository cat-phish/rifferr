"""Pure parts of S-LIBRARY-PRUNE: which albums have lost all their files, and the
review decision that lists them.

Nothing here touches the filesystem: the caller passes an existence check that answers
``True`` (present), ``False`` (missing) or ``None`` (can't tell, counts as present).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from rifferr.core.snapshot import AlbumSnapshot, label

PruneState = Literal["pending", "accepted", "rejected", "cancelled"]
PruneChoice = Literal["accept", "reject"]
Exists = Callable[[str], bool | None]


class PruneAlbum(BaseModel):
    """One album listed by the step (LIBRARY-PRUNE-1, -3)."""

    album_id: str
    label: str
    path: str | None = None
    items: int = 0
    missing: int = 0


class PruneDecision(BaseModel):
    """Library entries waiting for Accept or Reject in the review queue (LIBRARY-PRUNE-3)."""

    id: str
    profile: str
    run_id: str
    step_id: str
    state: PruneState = "pending"
    albums: list[PruneAlbum]
    """Albums whose files are all gone: these are dropped on accept."""
    partial: list[PruneAlbum] = Field(default_factory=list[PruneAlbum])
    """Albums with only some files gone: listed, never dropped."""
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None
    result: dict[str, list[str]] | None = None
    """``removed`` and ``skipped`` album ids once applied (LIBRARY-PRUNE-5)."""

    @property
    def lane_key(self) -> str:
        """The lane a pass without albums waits in (LANES-16)."""
        return f"pending:prune:{self.id}"


def entry(album: AlbumSnapshot, missing: int) -> PruneAlbum:
    return PruneAlbum(
        album_id=album.id,
        label=label(album),
        path=album.path,
        items=len(album.items),
        missing=missing,
    )


def all_missing(album: AlbumSnapshot, exists: Exists) -> bool:
    """Every item's file is missing for sure; an unknown answer counts as present."""
    return bool(album.items) and all(exists(i.path) is False for i in album.items)


def find_missing(
    albums: Iterable[AlbumSnapshot], exists: Exists
) -> tuple[list[PruneAlbum], list[PruneAlbum]]:
    """Albums whose files are all gone, and albums with only some gone (LIBRARY-PRUNE-1,
    -2). Albums without items are left out: there is nothing to check."""
    gone: list[PruneAlbum] = []
    partial: list[PruneAlbum] = []
    for album in albums:
        if not album.items:
            continue
        missing = sum(1 for i in album.items if exists(i.path) is False)
        if missing == len(album.items):
            gone.append(entry(album, missing))
        elif missing:
            partial.append(entry(album, missing))
    return gone, partial
