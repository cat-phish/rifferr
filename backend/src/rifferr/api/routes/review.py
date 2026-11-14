"""The review queue (F-REVIEW-QUEUE): what waits for a decision, the decisions, and
run undo.

Decisions reach the worker as jobs (CHANGESET-13); the server never applies anything.
Current values come from the profile's read-only worker (WORKER-11).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    ChangeSetDetail,
    CurrentValues,
    Decision,
    GateDecisionBody,
    ImportDecisionBody,
    ImportLookupBody,
    JobQueued,
    JobsQueued,
    QueueItem,
    RunDecision,
)
from rifferr.api.readonly import READ_ONLY_RESPONSES, ask
from rifferr.api.state import State
from rifferr.appdb import (
    HOLDING_CHANGESETS,
    AppChangeSets,
    AppImportDecisions,
    AppJournal,
    AppReviewGates,
    GateResolvedError,
    ImportBusyError,
    ImportResolvedError,
)
from rifferr.core.imports import ImportDecision
from rifferr.core.review import ReviewGate
from rifferr.engine.changesets import StoredChangeSet

router = APIRouter(prefix="/api", tags=["review"])

DECIDER = "ui"
"""Who decided, for every decision made through the API: there are no user accounts
in M1 (REVIEW-QUEUE-9)."""
CURRENT_TIMEOUT = 30.0
"""Seconds the read-only worker may take for current values (REVIEW-QUEUE-3)."""


@router.get("/review", response_model=list[QueueItem])
async def review_queue(
    state: State,
    profile: Annotated[str | None, Query()] = None,
    run: Annotated[str | None, Query()] = None,
) -> list[QueueItem]:
    """What waits for a decision, oldest first: run ChangeSets that are proposed or
    conflicted, pending review gates (REVIEW-QUEUE-1) and pending import decisions
    (IMPORT-UI-7)."""
    workflows: dict[str, str | None] = {}

    def workflow(run_id: str) -> str | None:
        if run_id not in workflows:
            job = state.db.job_for_run(run_id)
            workflows[run_id] = job.workflow if job is not None else None
        return workflows[run_id]

    items: list[QueueItem] = []
    for stored in AppChangeSets(state.db).waiting(profile, run):
        cs = stored.changeset
        assert cs.run_id is not None
        items.append(
            QueueItem(
                kind="changeset",
                id=cs.id,
                profile=cs.profile,
                run_id=cs.run_id,
                workflow=workflow(cs.run_id),
                step_id=cs.step_id,
                created_at=cs.created_at,
                changeset=cs,
                state=stored.state,
            )
        )
    for gate in AppReviewGates(state.db).pending(profile, run):
        items.append(
            QueueItem(
                kind="gate",
                id=gate.id,
                profile=gate.profile,
                run_id=gate.run_id,
                workflow=workflow(gate.run_id),
                step_id=gate.step_id,
                created_at=gate.created_at,
                gate=gate,
            )
        )
    for decision in AppImportDecisions(state.db).pending(profile=profile, run_id=run):
        items.append(
            QueueItem(
                kind="import",
                id=decision.id,
                profile=decision.profile,
                run_id=decision.run_id,
                workflow=workflow(decision.run_id),
                step_id=decision.step_id,
                created_at=decision.created_at,
                import_decision=decision,
            )
        )
    items.sort(key=lambda i: (i.created_at, i.id))
    return items


def _stored(state: State, changeset_id: str) -> StoredChangeSet:
    stored = AppChangeSets(state.db).get(changeset_id)
    if stored is None:
        raise not_found(f"ChangeSet {changeset_id}")
    return stored


@router.get("/changesets/{changeset_id}", response_model=ChangeSetDetail, responses=ERROR_RESPONSES)
async def get_changeset(changeset_id: str, state: State) -> ChangeSetDetail:
    """A ChangeSet with its state, decisions and journal entries (REVIEW-QUEUE-2)."""
    stored = _stored(state, changeset_id)
    return ChangeSetDetail(
        changeset=stored.changeset,
        state=stored.state,
        decisions=stored.decisions,
        resolved_by=stored.resolved_by,
        note=AppChangeSets(state.db).note(changeset_id),
        journal_ids=[e.id for e in AppJournal(state.db).for_changeset(changeset_id)],
    )


@router.get(
    "/changesets/{changeset_id}/current",
    response_model=CurrentValues,
    responses=READ_ONLY_RESPONSES,
)
async def current(changeset_id: str, state: State) -> CurrentValues:
    """The current values of the fields the ChangeSet changes, per target; writes
    nothing (REVIEW-QUEUE-3)."""
    cs = _stored(state, changeset_id).changeset
    result = await ask(
        state,
        cs.profile,
        "changes.current",
        cs.model_dump(mode="json"),
        timeout=CURRENT_TIMEOUT,
        timeout_code="query_timeout",
    )
    return CurrentValues.model_validate(result)


def _reviewable(stored: StoredChangeSet) -> None:
    if stored.changeset.run_id is None:
        # Manual ChangeSets apply at once and never wait for review (REVIEW-QUEUE-1).
        raise not_found(f"ChangeSet {stored.changeset.id} in the review queue")
    if stored.state not in HOLDING_CHANGESETS:
        raise ApiError(
            409, "already_resolved", f"ChangeSet {stored.changeset.id} is {stored.state}"
        )


@router.post(
    "/changesets/{changeset_id}/decision",
    status_code=202,
    response_model=JobQueued,
    responses=ERROR_RESPONSES,
)
async def decide(changeset_id: str, body: Decision, state: State) -> JobQueued:
    """Queue a decision job (REVIEW-QUEUE-4, CHANGESET-9, -13)."""
    stored = _stored(state, changeset_id)
    _reviewable(stored)
    accept = body.decision == "accept"
    if accept and stored.state == "conflicted":
        raise ApiError(
            409, "conflicted", f"ChangeSet {changeset_id} conflicted; reject it or propose again"
        )
    try:
        job_id = await state.supervisor.decide(
            changeset_id,
            accept=accept,
            accepted=body.accepted if accept else None,
            by=DECIDER,
            note=body.note,
        )
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    return JobQueued(job_id=job_id)


@router.post(
    "/changesets/{changeset_id}/repropose",
    status_code=202,
    response_model=JobQueued,
    responses=ERROR_RESPONSES,
)
async def repropose(changeset_id: str, state: State) -> JobQueued:
    """Propose a conflicted ChangeSet again on the current values (REVIEW-QUEUE-5)."""
    stored = _stored(state, changeset_id)
    _reviewable(stored)
    if stored.state != "conflicted":
        raise ApiError(409, "not_conflicted", f"ChangeSet {changeset_id} is {stored.state}")
    try:
        job_id = await state.supervisor.repropose(changeset_id, by=DECIDER)
    except ValueError as exc:
        raise ApiError(409, "not_conflicted", str(exc)) from None
    return JobQueued(job_id=job_id)


@router.post("/reviews/{gate_id}/decision", response_model=ReviewGate, responses=ERROR_RESPONSES)
async def decide_gate(gate_id: str, body: GateDecisionBody, state: State) -> ReviewGate:
    """Continue or reject a review gate (REVIEW-QUEUE-6, S-REVIEW REVIEW-6 .. -8)."""
    try:
        await state.supervisor.decide_gate(gate_id, body.decision, by=DECIDER, note=body.note)
    except KeyError:
        raise not_found(f"review gate {gate_id}") from None
    except GateResolvedError as exc:
        raise ApiError(409, "already_resolved", str(exc)) from None
    except ValueError as exc:
        raise ApiError(409, "not_waiting", str(exc)) from None
    gate = AppReviewGates(state.db).get(gate_id)
    assert gate is not None
    return gate


@router.post(
    "/runs/{run_id}/decisions",
    status_code=202,
    response_model=JobsQueued,
    responses=ERROR_RESPONSES,
)
async def decide_run(run_id: str, body: RunDecision, state: State) -> JobsQueued:
    """Decide every proposed ChangeSet of the run the same way, one job each; conflicted
    ones are left alone (REVIEW-QUEUE-7)."""
    if state.db.job_for_run(run_id) is None:
        raise not_found(f"run {run_id}")
    job_ids: list[str] = []
    for stored in AppChangeSets(state.db).for_run(run_id, "proposed"):
        try:
            job_ids.append(
                await state.supervisor.decide(
                    stored.changeset.id, accept=body.decision == "accept", by=DECIDER
                )
            )
        except ValueError:  # decided meanwhile
            continue
    return JobsQueued(job_ids=job_ids)


@router.post(
    "/runs/{run_id}/undo", status_code=202, response_model=JobQueued, responses=ERROR_RESPONSES
)
async def undo_run(run_id: str, state: State) -> JobQueued:
    """Queue the undo of the run's applied changes, newest first (REVIEW-QUEUE-8,
    JOURNAL-6). The result arrives as ``job.finished`` on the run's stream."""
    try:
        job_id = await state.supervisor.undo(run_id=run_id, by=DECIDER)
    except KeyError:
        raise not_found(f"run {run_id}") from None
    except ValueError as exc:
        raise ApiError(409, "not_undoable", str(exc)) from None
    return JobQueued(job_id=job_id)


# -- import decisions (F-IMPORT-UI) ----------------------------------------------------


@router.get("/imports/{decision_id}", response_model=ImportDecision, responses=ERROR_RESPONSES)
async def get_import(decision_id: str, state: State) -> ImportDecision:
    """The full import decision (IMPORT-UI-7)."""
    decision = AppImportDecisions(state.db).get(decision_id)
    if decision is None:
        raise not_found(f"import decision {decision_id}")
    return decision


@router.post(
    "/imports/{decision_id}/decision",
    status_code=202,
    response_model=JobQueued,
    responses=ERROR_RESPONSES,
)
async def decide_import(decision_id: str, body: ImportDecisionBody, state: State) -> JobQueued:
    """Answer an import decision, once; a worker job imports the folder (IMPORT-UI-8,
    -11). The result arrives as ``job.finished`` and ``import.resolved``."""
    try:
        job_id = await state.supervisor.decide_import(
            decision_id,
            body.action,
            candidate_id=body.candidate_id,
            by=DECIDER,
            note=body.note,
        )
    except KeyError:
        raise not_found(f"import decision {decision_id}") from None
    except ImportResolvedError as exc:
        raise ApiError(409, "already_resolved", str(exc)) from None
    except ImportBusyError as exc:
        raise ApiError(409, "busy", str(exc)) from None
    except ValueError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    return JobQueued(job_id=job_id)


@router.post(
    "/imports/{decision_id}/lookup",
    status_code=202,
    response_model=JobQueued,
    responses=ERROR_RESPONSES,
)
async def lookup_import(decision_id: str, body: ImportLookupBody, state: State) -> JobQueued:
    """Look the folder up again by release ids or by artist and album (IMPORT-UI-9)."""
    try:
        job_id = await state.supervisor.lookup_import(
            decision_id,
            ids=[i.strip() for i in body.ids if i.strip()],
            artist=(body.artist or "").strip() or None,
            album=(body.album or "").strip() or None,
        )
    except KeyError:
        raise not_found(f"import decision {decision_id}") from None
    except ImportResolvedError as exc:
        raise ApiError(409, "already_resolved", str(exc)) from None
    except ImportBusyError as exc:
        raise ApiError(409, "busy", str(exc)) from None
    return JobQueued(job_id=job_id)
