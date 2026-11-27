"""Shared AI routes: preview and bulk-job status/cancel (C-AI-CORE AI-CORE-18, -19).

Each AI feature starts its own bulk jobs from its own route; these routes only show what
a task would send, and follow or cancel a running job. Mutating-endpoint rules (JSON
only, Origin check) apply through the security middleware.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from pydantic import Field, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.ai.base import AITaskResult
from rifferr.ai.jobs import AIJob, Work
from rifferr.ai.runner import AIPreview, AITaskError, usage_json
from rifferr.ai.service import AIService
from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import _Model  # pyright: ignore[reportPrivateUsage]
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import ServerState, State, publish_profile_event
from rifferr.appdb import AppChoiceReviews, ChoiceBusyError, ChoiceResolvedError
from rifferr.core.choices import SUGGEST_TASK, ChoiceAI, ChoiceReview, ChoiceSuggestInput

router = APIRouter(prefix="/api", tags=["ai"])


class AIPreviewBody(_Model):
    profile: str = Field(max_length=200)
    task: str = Field(max_length=200)
    input: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    provider: str | None = Field(default=None, max_length=64)


class AIJobStatus(_Model):
    job: str
    task: str
    state: str
    total: int
    done: int
    failed: int
    skipped: int
    usage: dict[str, JsonValue]
    errors: dict[str, str]


def ai_service(state: ServerState) -> AIService:
    if state.ai is None:
        raise ApiError(503, "ai_unavailable", "the AI service is not running")
    return state.ai


def _status(job: AIJob) -> AIJobStatus:
    summary: dict[str, Any] = job.summary()
    return AIJobStatus(**summary, errors=dict(job.errors))


@router.post("/ai/preview", response_model=AIPreview, responses=ERROR_RESPONSES)
async def preview(body: AIPreviewBody, state: State) -> AIPreview:
    """Exactly what an AI task would send for ``input``; contacts no one (AI-CORE-18)."""
    service = ai_service(state)
    get_profile(state, body.profile)  # 404 / 422 for unknown or broken profiles
    task = service.task(body.task)
    if task is None:
        raise not_found(f"AI task '{body.task}'")
    try:
        inp = task.input_model.model_validate(body.input)
    except PydanticValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(x) for x in first["loc"])
        raise ApiError(400, "invalid_input", f"input.{where}: {first['msg']}") from None
    try:
        return service.runtime(body.profile).preview(task, inp, provider=body.provider)
    except AITaskError as exc:
        raise ApiError(400, "ai_unavailable", exc.message) from None


@router.get("/ai/jobs/{job_id}", response_model=AIJobStatus, responses=ERROR_RESPONSES)
async def get_job(job_id: str, state: State) -> AIJobStatus:
    job = ai_service(state).jobs.get(job_id)
    if job is None:
        raise not_found(f"AI job '{job_id}'")
    return _status(job)


@router.post(
    "/ai/jobs/{job_id}/cancel",
    status_code=202,
    response_model=AIJobStatus,
    responses=ERROR_RESPONSES,
)
async def cancel_job(job_id: str, state: State) -> AIJobStatus:
    """Stop starting new requests; running ones finish or time out (AI-CORE-19)."""
    job = ai_service(state).jobs.cancel(job_id)
    if job is None:
        raise not_found(f"AI job '{job_id}'")
    return _status(job)


# -- choice suggestions (F-AI-SUGGEST) -------------------------------------------------


class ChoiceSuggestBody(_Model):
    refresh: bool = Field(default=False, description="Ask again without the cache")


class ChoiceSuggestBulkBody(_Model):
    """Which waiting choice reviews to ask about: ``ids``, or all of a profile (one step)."""

    ids: list[str] | None = Field(default=None, max_length=1000)
    profile: str | None = Field(default=None, max_length=200)
    step_id: str | None = Field(default=None, max_length=200)
    refresh: bool = False


class ChoiceSuggestStarted(_Model):
    job: str | None
    total: int


class ChoiceSuggestEstimate(_Model):
    """What a bulk request would cost, before anything is sent (AI-CORE-19)."""

    total: int
    cached: int
    input_tokens: int
    max_output_tokens: int
    cost_max: Decimal | None = None
    currency: str | None = None


def _choices(state: ServerState) -> AppChoiceReviews:
    return AppChoiceReviews(state.db)


def _ready(review: ChoiceReview) -> ChoiceSuggestInput:
    if review.state != "pending":
        raise ApiError(409, "already_resolved", f"choice review {review.id} is {review.state}")
    if review.ai_input is None:
        raise ApiError(
            400,
            "no_ai",
            "this choice step has no ai block, or AI wasn't configured when it opened",
        )
    return review.ai_input


async def suggest_for(
    state: ServerState, review: ChoiceReview, *, refresh: bool
) -> AITaskResult[Any]:
    """Ask the AI about one review and store the answer (AI-SUGGEST-9, -10). Raises
    ``AITaskError``; a failure is stored on the review as its note."""
    service = ai_service(state)
    inp = _ready(review)
    task = service.task(SUGGEST_TASK)
    if task is None:
        raise AITaskError(f"AI task '{SUGGEST_TASK}' is not installed")
    store = _choices(state)
    try:
        result = await service.runtime(review.profile).run_task(
            task, inp, provider=inp.provider, refresh=refresh, target=review.album_id
        )
    except AITaskError as exc:
        kept = review.ai.suggestions if review.ai else []
        store.store_ai(review.id, ChoiceAI(suggestions=kept, error=exc.message, at=_now()))
        _announce(state, review, 0, exc.message)
        raise
    ai = ChoiceAI(
        suggestions=list(result.value),
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        usage=usage_json(result.usage),
        cached=result.cached,
        at=_now(),
    )
    store.store_ai(review.id, ai)
    _announce(state, review, len(ai.suggestions), None)
    return result


def _now() -> datetime:
    return datetime.now(UTC)


def _announce(state: ServerState, review: ChoiceReview, count: int, error: str | None) -> None:
    data: dict[str, JsonValue] = {
        "review_id": review.id,
        "album_id": review.album_id,
        "run_id": review.run_id,
        "count": count,
        "error": error,
    }
    publish_profile_event(state, review.profile, "choice.ai_suggested", data)


def _store_errors(exc: Exception, review_id: str) -> ApiError:
    if isinstance(exc, KeyError):
        return not_found(f"choice review '{review_id}'")
    if isinstance(exc, ChoiceResolvedError):
        return ApiError(409, "already_resolved", str(exc))
    if isinstance(exc, ChoiceBusyError):
        return ApiError(409, "busy", str(exc))
    raise exc


@router.post(
    "/choices/{review_id}/ai-suggest", response_model=ChoiceReview, responses=ERROR_RESPONSES
)
async def suggest_one(review_id: str, body: ChoiceSuggestBody, state: State) -> ChoiceReview:
    """Ask the AI for suggestions on one waiting choice review (AI-SUGGEST-10). Nothing is
    decided: the suggestions wait for the person (AI-SUGGEST-15)."""
    review = _choices(state).get(review_id)
    if review is None:
        raise not_found(f"choice review '{review_id}'")
    try:
        await suggest_for(state, review, refresh=body.refresh)
    except AITaskError as exc:
        raise ApiError(502, "ai_failed", exc.message) from None
    except (KeyError, ChoiceResolvedError, ChoiceBusyError) as exc:
        raise _store_errors(exc, review_id) from None
    found = _choices(state).get(review_id)
    assert found is not None
    return found


def _targets(state: ServerState, body: ChoiceSuggestBulkBody) -> list[ChoiceReview]:
    store = _choices(state)
    if body.ids is not None:
        found = [r for r in (store.get(i) for i in body.ids) if r is not None]
    elif body.profile is not None:
        found = store.pending(profile=body.profile)
    else:
        raise ApiError(400, "bad_request", "give ids or a profile")
    return [
        r
        for r in found
        if r.state == "pending"
        and r.ai_input is not None
        and (body.step_id is None or r.step_id == body.step_id)
        and (body.refresh or r.ai is None or not r.ai.suggestions)
    ]


@router.post(
    "/choices/ai-suggest/estimate",
    response_model=ChoiceSuggestEstimate,
    responses=ERROR_RESPONSES,
)
async def suggest_estimate(body: ChoiceSuggestBulkBody, state: State) -> ChoiceSuggestEstimate:
    """The count and estimated tokens (and cost when priced) for a bulk request, for the
    confirmation (AI-CORE-19, AI-SUGGEST-14). Sends nothing."""
    service = ai_service(state)
    task = service.task(SUGGEST_TASK)
    if task is None:
        raise ApiError(400, "ai_unavailable", f"AI task '{SUGGEST_TASK}' is not installed")
    out = ChoiceSuggestEstimate(total=0, cached=0, input_tokens=0, max_output_tokens=0)
    for review in _targets(state, body):
        assert review.ai_input is not None
        try:
            p = service.runtime(review.profile).preview(
                task, review.ai_input, provider=review.ai_input.provider
            )
        except AITaskError as exc:
            raise ApiError(400, "ai_unavailable", exc.message) from None
        out.total += 1
        if p.cached and not body.refresh:
            out.cached += 1
            continue
        out.input_tokens += p.estimated_input_tokens
        out.max_output_tokens += p.max_output_tokens
        if p.estimated_cost_max is not None:
            out.cost_max = (out.cost_max or Decimal(0)) + p.estimated_cost_max
            out.currency = p.currency
    return out


@router.post(
    "/choices/ai-suggest",
    status_code=202,
    response_model=ChoiceSuggestStarted,
    responses=ERROR_RESPONSES,
)
async def suggest_bulk(body: ChoiceSuggestBulkBody, state: State) -> ChoiceSuggestStarted:
    """Start an AI job over waiting choice reviews (AI-SUGGEST-11). Reviews that already
    have suggestions are skipped unless ``refresh``. Progress: ``ai.job.progress`` and each
    review's ``choice.ai_suggested``."""
    service = ai_service(state)
    targets = _targets(state, body)
    if not targets:
        return ChoiceSuggestStarted(job=None, total=0)
    profiles = {r.profile for r in targets}
    if len(profiles) != 1:
        raise ApiError(400, "bad_request", "ask about one profile's reviews at a time")
    profile = profiles.pop()
    concurrency = service.runtime(profile).settings.concurrency

    def work(review: ChoiceReview) -> Work:
        async def run() -> AITaskResult[Any]:
            current = _choices(state).get(review.id) or review
            return await suggest_for(state, current, refresh=body.refresh)

        return run

    job = service.jobs.start(
        profile, SUGGEST_TASK, [(r.id, work(r)) for r in targets], concurrency=concurrency
    )
    return ChoiceSuggestStarted(job=job.id, total=job.total)
