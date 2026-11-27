"""Shared AI routes: preview and bulk-job status/cancel (C-AI-CORE AI-CORE-18, -19).

Each AI feature starts its own bulk jobs from its own route; these routes only show what
a task would send, and follow or cancel a running job. Mutating-endpoint rules (JSON
only, Origin check) apply through the security middleware.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import Field, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.ai.jobs import AIJob
from rifferr.ai.runner import AIPreview, AITaskError
from rifferr.ai.service import AIService
from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import _Model  # pyright: ignore[reportPrivateUsage]
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import ServerState, State

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
