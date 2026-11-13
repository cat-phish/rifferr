"""Library browsing and manual edits from the tag grid (F-TAG-GRID).

Pure apart from the backend protocol: the read-only worker answers ``library_page``
and ``check_manual``; the server turns checked edits into ChangeSets with
``manual_changesets`` and the main worker applies them through the journal like
any other ChangeSet (TAG-GRID-8).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rifferr.backends.base import LibraryBackend
from rifferr.core.changeset import (
    ChangeProblem,
    ChangeSet,
    ChangeSource,
    FieldChange,
    TargetChange,
    TargetRef,
    same_value,
)
from rifferr.core.selection import Scope, Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue

LIBRARY_LIMIT: Final = 1000
MAX_LIBRARY_LIMIT: Final = 10000
MAX_MANUAL_TARGETS: Final = 10000
#: Provenance of grid edits (CHANGESET-1). Undo uses ``manual`` / ``undo``.
MANUAL_SOURCE: Final = ChangeSource(kind="manual", name="ui")

_FORBID = ConfigDict(extra="forbid")


class LibraryQuery(BaseModel):
    model_config = _FORBID

    query: str = Field(default="", description="beets query; empty lists the whole library")
    scope: Scope = "album"
    limit: int = Field(
        default=LIBRARY_LIMIT,
        ge=1,
        le=MAX_LIBRARY_LIMIT,
        description="Most albums (album scope) or items (item scope) to return",
    )


class LibraryPage(BaseModel):
    """Matching albums (TAG-GRID-2). In item scope each album has only its matching items."""

    model_config = _FORBID

    albums: list[AlbumSnapshot]
    album_fields: list[str] = Field(description="Album field names seen, sorted")
    item_fields: list[str] = Field(description="Item field names seen, sorted")
    truncated: bool = Field(description="More matched than 'limit'")


def library_page(backend: LibraryBackend, request: LibraryQuery) -> LibraryPage:
    """TAG-GRID-2: read only; ``QueryError`` propagates for a bad query."""
    albums = backend.query(Selection(query=request.query, scope=request.scope))
    truncated = False
    if request.scope == "album":
        truncated = len(albums) > request.limit
        albums = albums[: request.limit]
    else:
        kept: list[AlbumSnapshot] = []
        left = request.limit
        for album in albums:
            if left <= 0:
                truncated = True
                break
            if len(album.items) > left:
                truncated = True
                album = album.model_copy(update={"items": album.items[:left]})
            kept.append(album)
            left -= len(album.items)
        albums = kept
    album_fields = sorted({k for a in albums for k in a.fields})
    item_fields = sorted({k for a in albums for i in a.items for k in i.fields})
    return LibraryPage(
        albums=albums, album_fields=album_fields, item_fields=item_fields, truncated=truncated
    )


class ManualChange(BaseModel):
    """Pending edits to one album or item, as the grid sends them (TAG-GRID-7, -8)."""

    model_config = _FORBID

    target: TargetRef
    album: str = Field(
        min_length=1,
        description="Id of the album the target belongs to (the album itself for an album)",
    )
    changes: list[FieldChange] = Field(
        min_length=1, description="'old' is the value the user saw; null or absent = no value"
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.target.scope == "album" and self.album != self.target.id:
            raise ValueError("an album target's 'album' must be its own id")
        fields = [c.field for c in self.changes]
        if len(set(fields)) != len(fields):
            raise ValueError(f"{self.target.label()}: each field may be changed once")
        return self


class ManualChanges(BaseModel):
    model_config = _FORBID

    targets: list[ManualChange] = Field(min_length=1, max_length=MAX_MANUAL_TARGETS)
    rationale: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _unique_targets(self) -> Self:
        refs = [t.target for t in self.targets]
        if len(set(refs)) != len(refs):
            raise ValueError("each album or item may appear once")
        return self


class CheckResult(BaseModel):
    """Problems per target and field, and the edits with values coerced to their field
    types (TAG-GRID-7). Apply only when ``problems`` is empty."""

    model_config = _FORBID

    problems: list[ChangeProblem]
    targets: list[ManualChange]


def _coerce_new(backend: LibraryBackend, change: FieldChange) -> FieldValue:
    if change.op != "set":
        return None
    new = backend.coerce(change.field, change.new)
    if isinstance(change.new, str) and isinstance(new, list):
        # Multi-value fields take a list of entries; text is never split (TAG-GRID-5).
        return [change.new] if change.new else []
    return new


def _coerce_old(backend: LibraryBackend, change: FieldChange) -> FieldValue:
    """``old`` as the field's type: JSON from a browser turns ``200.0`` into ``200``."""
    if change.old is None:
        return None
    try:
        return backend.coerce(change.field, change.old)
    except ValueError:
        return change.old


def check_manual(backend: LibraryBackend, request: ManualChanges) -> CheckResult:
    """Coerce each value (BACKEND-11) and ``check`` the result (CHANGESET-2). Read only."""
    problems: list[ChangeProblem] = []
    coerced: list[ManualChange] = []
    for t in request.targets:
        changes: list[FieldChange] = []
        for c in t.changes:
            try:
                new = _coerce_new(backend, c)
            except ValueError as exc:
                problems.append(ChangeProblem(target=t.target, field=c.field, message=str(exc)))
                changes.append(c)
                continue
            changes.append(c.model_copy(update={"new": new, "old": _coerce_old(backend, c)}))
        coerced.append(t.model_copy(update={"changes": changes}))
    if not problems:
        for cs in manual_changesets(
            ManualChanges(targets=coerced),
            make_id=lambda: "check",
            profile="check",
            now=lambda: datetime.fromtimestamp(0),
        ):
            problems.extend(backend.check(cs))
    return CheckResult(problems=problems, targets=coerced)


def manual_changesets(
    request: ManualChanges,
    *,
    make_id: Callable[[], str],
    profile: str,
    now: Callable[[], datetime],
) -> list[ChangeSet]:
    """One ChangeSet per album (CHANGESET-14), source ``manual`` / ``ui``, no run.

    Each field's ``old`` is the value the user saw, so changes made meanwhile come
    back as conflicts (CHANGESET-8). Edits that set the value the user saw are
    dropped (CHANGESET-4).
    """
    groups: dict[str, list[TargetChange]] = {}
    for t in request.targets:
        kept = [
            c
            for c in t.changes
            if not (c.op == "set" and c.old is not None and same_value(c.old, c.new))
            and not (c.op == "delete" and c.old is None)
        ]
        if kept:
            groups.setdefault(t.album, []).append(TargetChange(target=t.target, changes=kept))
    return [
        ChangeSet(
            id=make_id(),
            profile=profile,
            source=MANUAL_SOURCE,
            rationale=request.rationale,
            created_at=now(),
            targets=targets,
        )
        for targets in groups.values()
    ]
