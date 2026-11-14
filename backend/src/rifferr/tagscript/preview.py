"""Tag-script preview (TAGSCRIPT-20): evaluate a script without proposing anything. Pure.

The response models are part of the HTTP API (``POST
/api/profiles/{profile}/tagscript/preview``) and the read-only worker's reply.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from rifferr.core.changeset import TargetRef
from rifferr.core.selection import Scope
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, label
from rifferr.core.workflow.errors import ValidationError
from rifferr.tagscript.evaluator import Evaluator, check_idempotence, run_script
from rifferr.tagscript.models import TagScript

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
TIMEOUT = 10.0
"""Seconds before a preview answers ``504 preview_timeout`` (TAGSCRIPT-21)."""

_MODEL = ConfigDict(frozen=True, extra="forbid")


class PreviewRequest(BaseModel):
    """A script as text (the editor's buffer) or a file in ``scripts/``, and a query."""

    model_config = _MODEL

    script_text: str | None = Field(default=None, description="YAML text of a script")
    script: str | None = Field(default=None, description="A file in the profile's scripts/")
    query: str = Field(default="", description="beets query selecting the albums")
    scope: Scope | None = Field(default=None, description="Override the script's scope")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Albums at most")
    client: str | None = Field(
        default=None,
        max_length=100,
        description="Caller id; a newer preview from the same client cancels an older one",
    )
    seq: int | None = Field(
        default=None,
        ge=0,
        description="Increases with each request from this client; says which is newer",
    )


class PreviewField(BaseModel):
    model_config = _MODEL

    field: str
    before: FieldValue
    after: FieldValue
    deleted: bool = False
    actions: list[str]


class PreviewTarget(BaseModel):
    model_config = _MODEL

    target: TargetRef
    album: str = Field(description="Id of the album this target belongs to")
    label: str
    fields: list[PreviewField] = Field(default_factory=list[PreviewField])
    actions: list[str] = Field(
        default_factory=list[str], description="Actions that changed this target"
    )
    errors: list[str] = Field(default_factory=list[str])
    notes: list[str] = Field(default_factory=list[str])


class PreviewIssue(BaseModel):
    """A field a second pass changes (TAGSCRIPT-16)."""

    model_config = _MODEL

    target: TargetRef
    field: str
    action: str | None
    action_index: int | None
    first: FieldValue
    second: FieldValue


class PreviewError(BaseModel):
    model_config = _MODEL

    path: str
    message: str
    line: int | None = None
    column: int | None = None

    @classmethod
    def of(cls, error: ValidationError) -> PreviewError:
        return cls(path=error.path, message=error.message, line=error.line, column=error.column)


class PreviewResponse(BaseModel):
    model_config = _MODEL

    targets: list[PreviewTarget] = Field(default_factory=list[PreviewTarget])
    idempotence: list[PreviewIssue] = Field(default_factory=list[PreviewIssue])
    idempotence_checked: bool = False
    validation_errors: list[PreviewError] = Field(default_factory=list[PreviewError])
    truncated: bool = False
    albums: int = Field(default=0, description="Albums evaluated")


def preview(
    script: TagScript,
    albums: list[AlbumSnapshot],
    ev: Evaluator,
    *,
    limit: int = DEFAULT_LIMIT,
    cancelled: Callable[[], bool] = lambda: False,
) -> PreviewResponse:
    """Evaluate ``script`` on up to ``limit`` albums; nothing is proposed or written."""
    targets: list[PreviewTarget] = []
    issues: list[PreviewIssue] = []
    chosen = albums[:limit]
    done = 0
    for album in chosen:
        if cancelled():
            break
        done += 1
        result = run_script(script, album, ev)
        by_ref: dict[TargetRef, list[PreviewField]] = {}
        hit: dict[TargetRef, list[str]] = {}
        for d in result.diffs():
            names = [result.label(i) for i in d.actions]
            by_ref.setdefault(d.target, []).append(
                PreviewField(
                    field=d.field,
                    before=d.before,
                    after=None if d.deleted else d.after,
                    deleted=d.deleted,
                    actions=list(dict.fromkeys(names)),
                )
            )
        for (ref, _field), indexes in result.hits.items():
            hit.setdefault(ref, [])
            for i in indexes:
                name = result.label(i)
                if name not in hit[ref]:
                    hit[ref].append(name)
        errors: dict[TargetRef, list[str]] = {}
        if result.error is not None:
            errors.setdefault(result.error.target, []).append(str(result.error))
            by_ref = {}  # TAGSCRIPT-15: nothing would be proposed for this album
        notes: dict[TargetRef, list[str]] = {}
        for n in result.notes:
            notes.setdefault(n.target, []).append(f"{n.action}: {n.message}")
        if result.error is None and script.idempotence != "off":
            check = check_idempotence(script, result, ev)
            if check.error is not None:
                errors.setdefault(check.error.target, []).append(
                    f"second pass fails: {check.error}"
                )
            issues += [
                PreviewIssue(
                    target=i.target,
                    field=i.field,
                    action=i.action,
                    action_index=i.action_index,
                    first=i.first,
                    second=i.second,
                )
                for i in check.issues
            ]
        for snapshot in [album, *album.items]:
            ref = TargetRef(
                scope="album" if isinstance(snapshot, AlbumSnapshot) else "item", id=snapshot.id
            )
            targets.append(
                PreviewTarget(
                    target=ref,
                    album=album.id,
                    label=label(snapshot),
                    fields=by_ref.get(ref, []),
                    actions=hit.get(ref, []) if ref in by_ref else [],
                    errors=errors.get(ref, []),
                    notes=notes.get(ref, []),
                )
            )
    return PreviewResponse(
        targets=targets,
        idempotence=issues,
        idempotence_checked=script.idempotence != "off",
        truncated=len(albums) > limit or done < len(chosen),
        albums=done,
    )
