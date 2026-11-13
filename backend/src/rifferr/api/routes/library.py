"""The library browser and manual edits (F-TAG-GRID).

Reads go to the profile's read-only worker, so they answer while a run imports
(WORKER-11). Edits become manual ChangeSets that the profile's worker applies
through the journal (TAG-GRID-8); their events go on the profile stream (TAG-GRID-9).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import JsonValue

from rifferr.api import sse
from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import ChangesCreated, JobQueued, ManualEdit
from rifferr.api.readonly import READ_ONLY_RESPONSES, ask
from rifferr.api.routes.runs import SSE_RESPONSE
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import State
from rifferr.appdb import AppChangeSets, AppJournal
from rifferr.core.ids import new_ulid
from rifferr.core.selection import Scope
from rifferr.engine.events import profile_stream
from rifferr.engine.manual import (
    LIBRARY_LIMIT,
    MANUAL_SOURCE,
    MAX_LIBRARY_LIMIT,
    CheckResult,
    LibraryPage,
    LibraryQuery,
    ManualChanges,
    manual_changesets,
)

router = APIRouter(prefix="/api/profiles/{profile}", tags=["library"])

QUERY_TIMEOUT = 30.0
"""Seconds a library query or check may take (TAG-GRID-2)."""


@router.get("/library", response_model=LibraryPage, responses=READ_ONLY_RESPONSES)
async def library(
    profile: str,
    state: State,
    query: Annotated[str, Query(description="beets query")] = "",
    scope: Annotated[Scope, Query()] = "album",
    limit: Annotated[int, Query(ge=1, le=MAX_LIBRARY_LIMIT)] = LIBRARY_LIMIT,
    client: Annotated[
        str | None, Query(max_length=100, description="A newer query from it cancels this one")
    ] = None,
) -> LibraryPage:
    """Albums or items matching a beets query; writes nothing (TAG-GRID-2)."""
    get_profile(state, profile)
    request = LibraryQuery(query=query, scope=scope, limit=limit)
    result = await ask(
        state,
        profile,
        "library.query",
        request.model_dump(mode="json"),
        timeout=QUERY_TIMEOUT,
        timeout_code="query_timeout",
        client=client,
    )
    return LibraryPage.model_validate(result)


async def _check(state: State, profile: str, body: ManualChanges) -> CheckResult:
    result = await ask(
        state,
        profile,
        "changes.check",
        body.model_dump(mode="json"),
        timeout=QUERY_TIMEOUT,
        timeout_code="query_timeout",
    )
    return CheckResult.model_validate(result)


@router.post("/changes/check", response_model=CheckResult, responses=READ_ONLY_RESPONSES)
async def check_changes(profile: str, body: ManualChanges, state: State) -> CheckResult:
    """Coerce each value to its field's type and check the edits; changes nothing
    (TAG-GRID-7)."""
    get_profile(state, profile)
    return await _check(state, profile, body)


@router.post(
    "/changes", status_code=202, response_model=ChangesCreated, responses=READ_ONLY_RESPONSES
)
async def create_changes(profile: str, body: ManualChanges, state: State) -> ChangesCreated:
    """Turn edits into manual ChangeSets, one per album, and queue them to apply now
    (TAG-GRID-8). Checked again first: ``422 invalid_changes`` lists the problems."""
    get_profile(state, profile)
    checked = await _check(state, profile, body)
    if checked.problems:
        details = cast(JsonValue, [p.model_dump(mode="json") for p in checked.problems])
        raise ApiError(422, "invalid_changes", "some edits can't be applied", details)
    changesets = manual_changesets(
        ManualChanges(targets=checked.targets, rationale=body.rationale),
        make_id=new_ulid,
        profile=profile,
        now=lambda: datetime.now(UTC),
    )
    if not changesets:
        raise ApiError(400, "no_changes", "every edit sets the value the field already has")
    job_id, ahead = await state.supervisor.apply_manual(profile, changesets, by="local user")
    return ChangesCreated(changesets=[c.id for c in changesets], job_id=job_id, queued_behind=ahead)


@router.get("/changes", response_model=list[ManualEdit], responses=ERROR_RESPONSES)
async def recent_changes(
    profile: str, state: State, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[ManualEdit]:
    """The profile's manual ChangeSets from the grid, newest first (TAG-GRID-11)."""
    get_profile(state, profile)
    journal = AppJournal(state.db)
    out: list[ManualEdit] = []
    for stored in AppChangeSets(state.db).manual(profile, MANUAL_SOURCE.name, limit):
        entries = [e for e in journal.for_changeset(stored.changeset.id) if e.undo_of is None]
        applied = next((e for e in reversed(entries) if e.state == "applied"), None)
        out.append(
            ManualEdit(
                changeset=stored.changeset,
                state=stored.state,
                resolved_by=stored.resolved_by,
                journal_id=applied.id if applied is not None else None,
                undo_journal_id=applied.undone_by if applied is not None else None,
            )
        )
    return out


@router.post(
    "/journal/{entry_id}/undo", status_code=202, response_model=JobQueued, responses=ERROR_RESPONSES
)
async def undo_entry(profile: str, entry_id: str, state: State) -> JobQueued:
    """Queue the undo of one journal entry (JOURNAL-4, -5). The result arrives as
    ``job.finished`` on the profile stream (TAG-GRID-11)."""
    get_profile(state, profile)
    entry = AppJournal(state.db).get(entry_id)
    if entry is None or entry.profile != profile:
        raise not_found(f"journal entry {entry_id} in profile '{profile}'")
    try:
        job_id = await state.supervisor.undo(entry_id=entry_id, by="local user")
    except ValueError as exc:
        raise ApiError(409, "not_undoable", str(exc)) from None
    return JobQueued(job_id=job_id)


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses={**SSE_RESPONSE, 404: ERROR_RESPONSES[404]},
)
async def profile_events(
    profile: str,
    state: State,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Events of the profile's work without a run: ``changeset.*`` of manual ChangeSets
    and ``job.finished`` (TAG-GRID-9). Replays after ``Last-Event-ID`` (API-8)."""
    get_profile(state, profile)
    try:
        after = max(int(last_event_id or 0), 0)
    except ValueError:
        raise ApiError(400, "bad_request", "Last-Event-ID must be an event seq") from None
    return StreamingResponse(
        sse.run_events(state, profile_stream(profile), after),
        media_type="text/event-stream",
        headers=sse.STREAM_HEADERS,
    )
