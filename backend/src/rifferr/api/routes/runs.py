from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import JsonValue

from rifferr.api import sse
from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    Accepted,
    RunCreate,
    RunCreated,
    RunDetail,
    RunSummary,
    StepState,
    StepStatusOut,
)
from rifferr.api.routes.workflows import check, error_out, find_workflow, get_profile
from rifferr.api.state import ServerState, State
from rifferr.appdb import Job
from rifferr.core.workflow import WorkflowLoadError, load_workflow

router = APIRouter(prefix="/api", tags=["runs"])

SSE_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}, "description": "SSE"}
}
TERMINAL = {"succeeded", "failed", "cancelled"}


@router.post("/runs", status_code=201, response_model=RunCreated, responses=ERROR_RESPONSES)
async def create_run(body: RunCreate, state: State) -> RunCreated:
    """Validate and queue a run (API-5). Trust is decided by the supervisor from the hash."""
    profile = get_profile(state, body.profile)
    path = find_workflow(state, body.profile, body.workflow)
    c = check(state, profile, body.workflow, path, body.vars, for_start=True)
    if body.sha256 is not None and c.sha256 != body.sha256:
        raise ApiError(409, "hash_mismatch", "the workflow changed since it was shown; reload it")
    if c.errors:
        details = cast(JsonValue, [e.model_dump() for e in error_out(c.errors)])
        raise ApiError(422, "validation_failed", "the workflow has errors", details)
    run_id = await state.supervisor.submit_run(
        body.profile, c.text, source_name=str(path), run_vars=body.vars, workflow=body.workflow
    )
    return RunCreated(id=run_id)


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(
    state: State,
    profile: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[RunSummary]:
    return [summary(job) for job in state.db.list_runs(profile, limit)]


@router.get("/runs/{run_id}", response_model=RunDetail, responses=ERROR_RESPONSES)
async def get_run(run_id: str, state: State) -> RunDetail:
    job = state.db.job_for_run(run_id)
    if job is None:
        raise not_found(f"run {run_id}")
    return RunDetail(**summary(job).model_dump(), steps=steps(state, job))


@router.post(
    "/runs/{run_id}/cancel", status_code=202, response_model=Accepted, responses=ERROR_RESPONSES
)
async def cancel_run(run_id: str, state: State) -> Accepted:
    """Request cancellation (API-7, RUN-8). No effect on a finished run."""
    try:
        await state.supervisor.cancel(run_id)
    except KeyError:
        raise not_found(f"run {run_id}") from None
    return Accepted(id=run_id)


@router.get(
    "/runs/{run_id}/events",
    response_class=StreamingResponse,
    responses={**SSE_RESPONSE, 404: ERROR_RESPONSES[404]},
)
async def run_events(
    run_id: str,
    state: State,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    if state.db.job_for_run(run_id) is None:
        raise not_found(f"run {run_id}")
    try:
        after = max(int(last_event_id or 0), 0)
    except ValueError:
        raise ApiError(400, "bad_request", "Last-Event-ID must be an event seq") from None
    return StreamingResponse(
        sse.run_events(state, run_id, after),
        media_type="text/event-stream",
        headers=sse.STREAM_HEADERS,
    )


@router.get("/events", response_class=StreamingResponse, responses=SSE_RESPONSE)
async def all_events(state: State) -> StreamingResponse:
    """run.started / run.finished across all runs (API-9)."""
    return StreamingResponse(
        sse.all_run_events(state), media_type="text/event-stream", headers=sse.STREAM_HEADERS
    )


# -- helpers --------------------------------------------------------------------


def summary(job: Job) -> RunSummary:
    return RunSummary(
        id=job.run_id,
        profile=job.profile,
        workflow=job.workflow,
        workflow_hash=job.workflow_hash,
        state=job.state,
        trusted=job.payload.get("trusted") is True,
        error=job.error,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def steps(state: ServerState, job: Job) -> list[StepState]:
    """Step list from the stored workflow text, statuses folded from events (API-6)."""
    try:
        wf, _ = load_workflow(str(job.payload.get("source", "")), "<run>")
    except WorkflowLoadError:
        return []
    out = {s.id: StepState(id=s.id, uses=s.uses, status="pending") for s in wf.steps}
    for event in state.db.events_since(job.run_id, 0):
        step = out.get(event.step_id or "")
        if step is None:
            continue
        if event.type == "run.step.started":
            step.status = "running"
        elif event.type == "run.step.finished":
            step.status = cast(StepStatusOut, event.data.get("status", "failed"))
            error = event.data.get("error")
            step.error = error if isinstance(error, str) else None
            outputs = event.data.get("outputs")
            step.outputs = cast(dict[str, JsonValue], outputs) if isinstance(outputs, dict) else {}
    if job.state in TERMINAL:
        for step in out.values():
            if step.status in ("pending", "running"):
                step.status = "skipped" if step.status == "pending" else "cancelled"
    return list(out.values())
