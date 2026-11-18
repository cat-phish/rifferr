"""The queue dashboard's own routes (F-DASHBOARD): inbox, recent imports, import undo.

Runs in progress and the review queue come from ``/api/runs`` and ``/api/review``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import APIRouter, Query

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    ImportedAlbum,
    ImportUndoBody,
    InboxEntry,
    InboxFolder,
    JobQueued,
    RecentImport,
)
from rifferr.api.readonly import READ_ONLY_RESPONSES, ask
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import State
from rifferr.appdb import AppTriggers, Job
from rifferr.worker.import_undo import Originals, deletes_files, originals

router = APIRouter(prefix="/api", tags=["dashboard"])

#: Undo and gate decisions record this decider (REVIEW-QUEUE-9).
DECIDER = "ui"
IMPORTS_TIMEOUT = 30.0
#: Runs read per recent-imports request, so runs without albums don't hide older ones.
SCAN_FACTOR = 3


@router.get(
    "/profiles/{profile}/inbox", response_model=list[InboxFolder], responses=ERROR_RESPONSES
)
async def inbox(profile: str, state: State) -> list[InboxFolder]:
    """Each watch trigger's folder and its inputs with their state (DASHBOARD-1, -2)."""
    get_profile(state, profile)
    if state.triggers is None:
        return []
    folders = await state.triggers.inbox(profile)
    return [
        InboxFolder(
            workflow=f.workflow,
            folder=f.folder,
            trigger_state=f.trigger_state,
            detail=f.detail,
            omitted=f.omitted,
            entries=[
                InboxEntry(path=e.path, state=e.state, changed_at=e.changed_at, run_id=e.run_id)
                for e in f.entries
            ],
        )
        for f in folders
    ]


def _album(raw: dict[str, Any], run: Job, dirs: list[Path], source: Originals) -> ImportedAlbum:
    items = [str(p) for p in raw.get("items") or []]
    busy = run.state in ("queued", "running")
    folder = raw.get("path") or (str(Path(items[0]).parent) if items else None)
    return ImportedAlbum(
        id=str(raw["id"]),
        label=str(raw.get("label", raw["id"])),
        items=len(items),
        undoable=not busy,
        reason=f"the run is {run.state}" if busy else None,
        deletes_files=deletes_files(items, dirs, source),
        files_at=folder,
    )


@router.get(
    "/profiles/{profile}/imports", response_model=list[RecentImport], responses=READ_ONLY_RESPONSES
)
async def recent_imports(
    profile: str, state: State, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> list[RecentImport]:
    """The latest runs whose imported albums are still in the library (DASHBOARD-6)."""
    get_profile(state, profile)
    runs = [j for j in reversed(state.db.jobs(profile)) if j.kind == "run"][: limit * SCAN_FACTOR]
    if not runs:
        return []
    answer = await ask(
        state,
        profile,
        "imports.recent",
        {"run_ids": [r.run for r in runs]},
        timeout=IMPORTS_TIMEOUT,
        timeout_code="query_timeout",
    )
    found = cast(dict[str, list[dict[str, Any]]], answer.get("runs") or {})
    dirs = [Path(str(d)) for d in cast(list[Any], answer.get("library_dirs") or [])]
    out: list[RecentImport] = []
    for run in runs:
        albums = found.get(run.run) or []
        if not albums:
            continue
        recorded = AppTriggers(state.db).run_input(run.run)
        source = await asyncio.to_thread(originals, recorded)
        out.append(
            RecentImport(
                run_id=run.run,
                workflow=run.workflow,
                input=run.input,
                state=run.state,
                finished_at=run.finished_at,
                albums=[_album(a, run, dirs, source) for a in albums],
            )
        )
        if len(out) == limit:
            break
    return out


@router.post(
    "/runs/{run_id}/import-undo",
    status_code=202,
    response_model=JobQueued,
    responses=ERROR_RESPONSES,
)
async def import_undo(run_id: str, body: ImportUndoBody, state: State) -> JobQueued:
    """Queue the removal of the run's imported albums, after undoing its changes to
    them (DASHBOARD-7). The report arrives as ``job.finished`` on the run's stream."""
    try:
        job_id = await state.supervisor.import_undo(run_id, albums=body.albums, by=DECIDER)
    except KeyError:
        raise not_found(f"run {run_id}") from None
    except ValueError as exc:
        raise ApiError(409, "not_undoable", str(exc)) from None
    return JobQueued(job_id=job_id)
