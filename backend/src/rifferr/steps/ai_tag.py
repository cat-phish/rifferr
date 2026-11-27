"""The ``ai.tag`` step: AI-proposed values for an allowlist of fields (S-AI-TAG).

One ``tag`` task run per album (AI-TAG-3). The answer is checked against the allowlist,
denylist, vocabulary and confidence floor by the task, then converted and checked by the
backend here (AI-TAG-7). What is left becomes one ChangeSet per album, which always waits
for review (AI-TAG-1, non-negotiable 4).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from rifferr.ai.tasks.tag import ALBUM_ID, TAG_TASK, TagInput, TagProposal, TagTarget
from rifferr.backends.base import BackendCapabilities, CapabilityError, LibraryBackend
from rifferr.core.ai_validate import CheckedValue, OnUnknown
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetAI,
    ChangeSetProposal,
    ChangeSource,
    FieldChange,
    FieldName,
    FieldValue,
    ProposedFieldChange,
    ProposedTargetChange,
    TargetChange,
    TargetRef,
)
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot
from rifferr.engine.steps import AIAnswer, AIUnavailableError, StepContext, StepResult
from rifferr.steps.covers_search import album_label

_PROPOSALS: TypeAdapter[list[TagProposal]] = TypeAdapter(list[TagProposal])


class AITagParams(BaseModel):
    """``with:`` of an ``ai.tag`` step (S-AI-TAG Interfaces)."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = Field(default=None, max_length=64)
    scope: Literal["album", "item"] = "album"
    fields: list[FieldName] = Field(min_length=1, description="The allowlist (AI-TAG-2)")
    context: list[FieldName] = Field(default_factory=list[str])
    item_context: list[FieldName] = Field(default_factory=list[str])
    rules: str | None = Field(default=None, description="Markdown notes, profile-relative")
    vocabulary: dict[str, str] = Field(default_factory=dict[str, str])
    denylist: dict[str, str] = Field(default_factory=dict[str, str])
    on_unknown_value: OnUnknown = "drop"
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    only_empty: bool = False
    when: str | None = Field(default=None, description="A beets query; others are skipped")
    retries: int = Field(default=2, ge=0, le=5)
    on_error: Literal["skip", "fail"] = "skip"
    refresh: bool = False

    @model_validator(mode="after")
    def _lists_name_fields(self) -> Self:
        for key, mapping in (("vocabulary", self.vocabulary), ("denylist", self.denylist)):
            extra = sorted(set(mapping) - set(self.fields))
            if extra:
                raise ValueError(f"{key} for {', '.join(extra)}, which isn't in fields (AI-TAG-6)")
        return self


def _json(value: FieldValue) -> JsonValue:
    if isinstance(value, list):
        return [str(v) for v in value]
    return value


def _empty(value: FieldValue) -> bool:
    return value is None or value == "" or value == []


class _Totals:
    def __init__(self) -> None:
        self.requests = self.cached = self.proposed = self.rejected = 0
        self.skipped = self.failed = 0
        self.input_tokens = self.output_tokens = 0
        self.cost: Decimal | None = Decimal(0)
        self.currency: str | None = None

    def add(self, answer: AIAnswer) -> None:
        if answer.cached:
            self.cached += 1
        else:
            self.requests += 1
        u = answer.usage
        self.input_tokens += int(u.get("input_tokens") or 0)  # pyright: ignore[reportArgumentType]
        self.output_tokens += int(u.get("output_tokens") or 0)  # pyright: ignore[reportArgumentType]
        raw = u.get("cost")
        if raw is None:
            self.cost = None  # one unknown cost makes the total unknown (AI-CORE-15)
        elif self.cost is not None:
            try:
                self.cost += Decimal(str(raw))
            except InvalidOperation:
                self.cost = None
        if isinstance(u.get("currency"), str):
            self.currency = str(u["currency"])

    def outputs(self) -> dict[str, str | int | float | bool]:
        cost = "" if self.cost is None else str(self.cost)
        return {
            "requests": self.requests,
            "cached": self.cached,
            "proposed": self.proposed,
            "rejected": self.rejected,
            "skipped": self.skipped,
            "failed": self.failed,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost": cost,
            "currency": self.currency or "",
        }


class AITagStep:
    name = "ai.tag"
    force_review = True  # AI output always goes to review (AI-TAG-1, CHANGESET-6)
    source_kind = "ai"
    waits = "apply"  # may put items in the review queue (UI-REFRESH-13)
    params_model = AITagParams
    requires = BackendCapabilities(query=True, apply=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, AITagParams)
        totals = _Totals()
        if not ctx.ai_configured:
            raise AIUnavailableError("ai.tag needs an AI provider in the profile's ai section")
        settings = ctx.profile_settings.ai if ctx.profile_settings is not None else None
        gate = asyncio.Semaphore(settings.concurrency if settings is not None else 2)
        multi = _Multi(ctx)

        async def one(album: AlbumSnapshot) -> None:
            async with gate:
                if ctx.cancelled:
                    return
                await _tag_album(ctx, params, album, totals, multi)

        await asyncio.gather(*(one(a) for a in ctx.albums()))
        return StepResult(outputs=dict(totals.outputs()))


class _Multi:
    """Which fields hold several values, asked of the backend once per field."""

    def __init__(self, ctx: StepContext) -> None:
        self._ctx = ctx
        self._known: dict[str, bool] = {}

    def __call__(self, field: str, album: AlbumSnapshot, ref: TargetRef) -> bool:
        if field not in self._known:
            self._known[field] = not _problems(self._ctx, ref, field, ["x"])
        return self._known[field]


def _probe(ctx: StepContext, ref: TargetRef, field: str, value: FieldValue) -> ChangeSet:
    return ChangeSet(
        id="ai-tag-check",
        profile=ctx.profile,
        source=ChangeSource(kind="ai", name="ai.tag"),
        created_at=datetime.now(UTC),
        targets=[TargetChange(target=ref, changes=[FieldChange(field=field, new=value)])],
    )


def _problems(ctx: StepContext, ref: TargetRef, field: str, value: FieldValue) -> list[str]:
    try:
        return [p.message for p in ctx.backend.check(_probe(ctx, ref, field, value))]
    except CapabilityError as exc:
        return [str(exc)]


def _targets(
    params: AITagParams, album: AlbumSnapshot
) -> list[tuple[TargetRef, AlbumSnapshot | ItemSnapshot]]:
    if params.scope == "album":
        return [(TargetRef(scope="album", id=album.id), album)]
    return [(TargetRef(scope="item", id=i.id), i) for i in album.items]


def _skip_reason(
    ctx: StepContext, params: AITagParams, album: AlbumSnapshot, backend: LibraryBackend
) -> str | None:
    if params.when is not None and not backend.matches(params.when, album):
        return f"doesn't match when: {params.when}"
    if params.only_empty:
        full = all(
            not _empty(snap.fields.get(f))
            for _, snap in _targets(params, album)
            for f in params.fields
        )
        if full:
            return "every field already has a value (only_empty)"
    return None


def _input(ctx: StepContext, params: AITagParams, album: AlbumSnapshot, multi: _Multi) -> TagInput:
    targets: list[TagTarget] = []
    for ref, snap in _targets(params, album):
        is_item = isinstance(snap, ItemSnapshot)
        label = str(snap.fields.get("title") or snap.id) if is_item else album_label(album)
        context = {f: _json(snap.fields.get(f)) for f in params.item_context} if is_item else {}
        targets.append(
            TagTarget(
                id=ref.id if is_item else ALBUM_ID,
                label=label,
                fields={f: _json(snap.fields.get(f)) for f in params.fields},
                context=context,
            )
        )
    first = _targets(params, album)[0][0] if targets else TargetRef(scope="album", id=album.id)
    return TagInput(
        scope=params.scope,
        album=album_label(album),
        album_context={f: _json(album.fields.get(f)) for f in params.context},
        fields=list(params.fields),
        multi=[f for f in params.fields if multi(f, album, first)],
        targets=targets,
        rules=params.rules,
        vocabulary=dict(params.vocabulary),
        denylist=dict(params.denylist),
        on_unknown_value=params.on_unknown_value,
        min_confidence=params.min_confidence,
        provider=params.provider,
    )


async def _tag_album(
    ctx: StepContext,
    params: AITagParams,
    album: AlbumSnapshot,
    totals: _Totals,
    multi: _Multi,
) -> None:
    skip = _skip_reason(ctx, params, album, ctx.backend)
    if skip is not None:
        totals.skipped += 1
        ctx.log(f"{album.id}: skipped, {skip}", target=album.id)
        return
    if params.scope == "item" and not album.items:
        totals.skipped += 1
        ctx.log(f"{album.id}: skipped, no tracks", target=album.id)
        return
    inp = _input(ctx, params, album, multi)
    try:
        answer = await ctx.ask_ai(
            TAG_TASK,
            inp,
            provider=params.provider,
            target=album.id,
            retries=params.retries,
            refresh=params.refresh,
        )
    except AIUnavailableError as exc:
        if params.on_error == "fail":
            totals.failed += 1
            ctx.fail_target(album.id, f"AI tagging failed: {exc}")
        else:
            totals.skipped += 1
            ctx.log(f"{album.id}: AI tagging failed, skipped: {exc}", "warning", album.id)
        return
    totals.add(answer)
    totals.rejected += len(answer.rejected)
    for r in answer.rejected:
        if r.get("reason") in ("unknown_target", "duplicate_target"):
            ctx.log(
                f"{album.id}: ignored the answer for unknown id {r.get('value')}",
                "warning",
                album.id,
            )
    refs = {
        (ALBUM_ID if ref.scope == "album" else ref.id): ref for ref, _ in _targets(params, album)
    }
    changes: list[ProposedTargetChange] = []
    for proposal in _PROPOSALS.validate_python(answer.value):
        ref = refs[proposal.id]
        kept = [c for v in proposal.values if (c := _change(ctx, ref, v, totals)) is not None]
        if kept:
            changes.append(ProposedTargetChange(target=ref, changes=kept))
    if not changes:
        ctx.log(f"{album.id}: nothing to propose; the answer had no usable values", target=album.id)
        return
    start = len(ctx.proposed)
    ctx.propose(
        ChangeSetProposal(
            rationale=f"AI tagging by {answer.provider} ({answer.model}), "
            f"prompt version {answer.prompt_version}",
            targets=changes,
        ),
        ai_input=ChangeSetAI(
            task=TAG_TASK, provider=params.provider, input=inp.model_dump(mode="json")
        ),
    )
    made = ctx.proposed[start:]
    totals.proposed += len(made)
    n = sum(len(cs.field_changes()) for cs in made)
    what = f"{n} field change{'s' if n != 1 else ''} proposed" if n else "no changes"
    ctx.log(f"{album.id}: {what}", target=album.id)


def _change(
    ctx: StepContext, ref: TargetRef, value: CheckedValue, totals: _Totals
) -> ProposedFieldChange | None:
    """Step 5 of AI-TAG-7: ``backend.coerce`` and ``backend.check``, each element of a list
    on its own; a field with nothing left is dropped."""
    kept: list[Any] = []
    for raw in value.values:
        if value.as_list:
            coerced: FieldValue = raw  # lists pass through coerce unchanged (BACKEND-11)
            probe: FieldValue = [raw]
        else:
            try:
                coerced = ctx.backend.coerce(value.field, raw)
            except ValueError as exc:
                _reject(ctx, ref, value.field, raw, f"invalid_value: {exc}", totals)
                continue
            probe = coerced
        problems = _problems(ctx, ref, value.field, probe)
        if problems:
            _reject(ctx, ref, value.field, raw, f"invalid_value: {problems[0]}", totals)
            continue
        kept.append(coerced)
    if not kept:
        return None
    new: FieldValue = kept if value.as_list else kept[0]
    return ProposedFieldChange(
        field=value.field,
        new=new,
        rationale=value.rationale or None,
        confidence=value.confidence,
        warnings=list(value.warnings),
    )


def _reject(
    ctx: StepContext, ref: TargetRef, field: str, raw: str, reason: str, totals: _Totals
) -> None:
    totals.rejected += 1
    ctx.ai_rejected(raw, reason.split(":", 1)[0], field=field, target=ref.id)
    ctx.log(f"{ref.id}: dropped {field} = {raw!r}: {reason}", "warning", ref.id)
