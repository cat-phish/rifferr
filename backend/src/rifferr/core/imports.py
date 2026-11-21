"""Import decisions (F-IMPORT-UI): albums an import asks about, and the answers.

Backend-neutral data: a backend's lookup produces ``ImportQuestion``s with candidates,
the engine stores them as ``ImportDecision``s, and a later apply imports the folder with
the chosen ``ImportAction``. Pure: no beets, no filesystem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rifferr.core.covers import web_url_or_none
from rifferr.core.snapshot import AlbumSnapshot, FieldValue

ImportDecisionKind = Literal["match", "duplicate"]
ImportDecisionState = Literal["pending", "applied", "skipped", "cancelled"]
ImportAction = Literal["candidate", "asis", "skip", "keep", "remove", "merge"]
TimeoutAction = Literal["skip", "asis"]

#: Actions for kind ``match`` and for kind ``duplicate`` (IMPORT-UI-3, -8).
MATCH_ACTIONS: Final = frozenset({"candidate", "asis", "skip"})
DUPLICATE_ACTIONS: Final = frozenset({"skip", "keep", "remove", "merge"})

#: Limits on lookup input (IMPORT-UI-9).
MAX_LOOKUP_IDS: Final = 10
MAX_LOOKUP_TEXT: Final = 200

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class FolderItem(BaseModel):
    """One file of the folder as it is now: its tags before any match."""

    model_config = _FROZEN

    path: str
    format: str | None = None
    length: float | None = Field(default=None, description="Seconds")
    tags: dict[str, FieldValue] = Field(default_factory=dict[str, FieldValue])


class TrackMatch(BaseModel):
    """How one file maps to a candidate's track."""

    model_config = _FROZEN

    item: str = Field(description="The file's path")
    title_before: str | None = None
    title_after: str | None = None
    track: int | None = Field(default=None, description="The candidate's track number")
    length: float | None = Field(default=None, description="The candidate track's seconds")


class MatchThresholds(BaseModel):
    """The backend's limits for a strong and a medium match, as distances (F-UI-REFRESH
    UI-REFRESH-6). beets: ``match.strong_rec_thresh`` and ``match.medium_rec_thresh``."""

    model_config = _FROZEN

    strong: float = Field(ge=0, le=1)
    medium: float = Field(ge=0, le=1)


class ImportCandidate(BaseModel):
    """One match the backend's lookup found, in the backend's order (IMPORT-UI-2, -4)."""

    model_config = _FROZEN

    id: str = Field(description="'<data_source>:<album id>', stable across lookups")
    source: str
    artist: str = ""
    album: str = ""
    year: int | None = None
    country: str | None = None
    label: str | None = None
    catalognum: str | None = None
    media: str | None = None
    tracks: int = 0
    distance: float = Field(ge=0, le=1, description="0 is a perfect match")
    penalties: list[str] = Field(default_factory=list[str])
    mapping: list[TrackMatch] = Field(default_factory=list[TrackMatch])
    missing: list[str] = Field(
        default_factory=list[str], description="Candidate tracks without a file"
    )
    unmatched: list[str] = Field(
        default_factory=list[str], description="Files without a candidate track"
    )
    url: str | None = None

    @field_validator("url")
    @classmethod
    def _web_link_only(cls, v: str | None) -> str | None:
        return web_url_or_none(v)  # SEC-44


class ImportQuestion(BaseModel):
    """What an import in ``auto``/``interactive`` mode asks about one album folder."""

    model_config = _FROZEN

    kind: ImportDecisionKind
    path: str
    fingerprint: str = Field(description="Of the folder's files, to detect changes")
    items: list[FolderItem] = Field(default_factory=list[FolderItem])
    candidates: list[ImportCandidate] = Field(default_factory=list[ImportCandidate])
    duplicates: list[AlbumSnapshot] = Field(
        default_factory=list[AlbumSnapshot], description="Kind duplicate: the albums it duplicates"
    )


class ImportDecision(BaseModel):
    """A stored import question and, once answered, its answer (IMPORT-UI-4, -12)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    profile: str
    run_id: str
    step_id: str
    kind: ImportDecisionKind
    path: str
    fingerprint: str
    items: list[FolderItem] = Field(default_factory=list[FolderItem])
    candidates: list[ImportCandidate] = Field(default_factory=list[ImportCandidate])
    duplicates: list[AlbumSnapshot] = Field(default_factory=list[AlbumSnapshot])
    lookup_problem: str | None = Field(
        default=None,
        description="Why the backend can't find candidates at all, e.g. no metadata source "
        "plugin loaded (IMPORT-UI-20); the queue shows it instead of 'no candidates'",
    )
    match_thresholds: MatchThresholds | None = Field(
        default=None,
        description="Distances up to which the backend calls a match strong or medium, for "
        "coloring candidates (UI-REFRESH-6); null when the backend has none",
    )
    options: dict[str, str | bool] = Field(default_factory=dict[str, str | bool])
    set_fields: dict[str, str] = Field(default_factory=dict[str, str])
    state: ImportDecisionState = "pending"
    error: str | None = None
    created_at: datetime
    deadline: datetime | None = None
    on_timeout: TimeoutAction = "skip"
    action: ImportAction | None = None
    candidate_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    note: str | None = None
    album_id: str | None = None

    @property
    def lane_key(self) -> str:
        return pending_key(self.id)

    def allows(self, action: str) -> bool:
        return action in (MATCH_ACTIONS if self.kind == "match" else DUPLICATE_ACTIONS)


def pending_key(decision_id: str) -> str:
    """The lane of an album not imported yet (C-LANES LANES-14)."""
    return f"pending:{decision_id}"


def merge_candidates(
    known: list[ImportCandidate], found: list[ImportCandidate]
) -> list[ImportCandidate]:
    """``known`` plus the candidates of ``found`` not listed yet (IMPORT-UI-9)."""
    seen = {c.id for c in known}
    return [*known, *(c for c in found if c.id not in seen)]


class ImportApplyError(RuntimeError):
    """Applying a decision failed; the decision stays pending (IMPORT-UI-11, -13)."""


__all__ = [
    "DUPLICATE_ACTIONS",
    "MATCH_ACTIONS",
    "MAX_LOOKUP_IDS",
    "MAX_LOOKUP_TEXT",
    "FolderItem",
    "ImportAction",
    "ImportApplyError",
    "ImportCandidate",
    "ImportDecision",
    "ImportDecisionKind",
    "ImportDecisionState",
    "ImportQuestion",
    "TimeoutAction",
    "TrackMatch",
    "merge_candidates",
    "pending_key",
]
