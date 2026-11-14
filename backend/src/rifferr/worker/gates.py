"""Ending lanes rejected at a review gate (REVIEW-8, C-LANES LANES-11), shared by server
and worker.

The server does it directly for ``on_reject: stop``; the worker does it after undoing
the gate's albums for ``on_reject: undo``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import JsonValue

from rifferr.appdb import AppChangeSets, AppDB, AppImportDecisions, AppLanes, AppReviewGates
from rifferr.core.review import REJECTED_AT_REVIEW, ReviewGate
from rifferr.engine.lanes import FINAL_LANE_STATES, lane_json

#: (event type, step id, data) -> None; puts an event on the run's stream.
EmitRun = Callable[[str, str | None, dict[str, JsonValue]], None]

Settled = Literal["not_waiting", "requeued", "waiting", "finished"]


@dataclass(frozen=True)
class RejectOutcome:
    settled: Settled
    lanes: list[str]
    """The lanes this rejection ended ``rejected``."""


def gate_albums(gate: ReviewGate) -> list[str]:
    return [a.target.id for a in gate.albums]


def reject_gate_lanes(db: AppDB, gate: ReviewGate, emit: EmitRun) -> RejectOutcome:
    """End the gate's albums' lanes ``rejected`` and settle the run (LANES-11).

    Their proposed ChangeSets become rejected, and gates only they waited for are
    cancelled. Then the run is requeued when another lane was released meanwhile, stays
    waiting while lanes still wait, or finishes (LANES-7, -9): ``cancelled`` with reason
    ``rejected_at_review`` when every lane was rejected, ``failed`` when any lane failed,
    otherwise ``succeeded``. A run without lanes (suspended before C-LANES) ends
    ``cancelled`` as a whole, as before.
    """
    run_id = gate.run_id
    job = db.job_for_run(run_id)
    if job is None or job.state != "waiting_review":
        return RejectOutcome("not_waiting", [])
    store = AppLanes(db)
    lanes = store.load(run_id)
    if not lanes:
        return RejectOutcome("finished" if _finish_whole(db, run_id, emit) else "not_waiting", [])
    keys = [k for k in gate_albums(gate) if k in lanes and not lanes[k].final]
    rejected = store.set_state(run_id, keys, "rejected", only_from=("waiting", "active"))
    changesets = AppChangeSets(db)
    gates = AppReviewGates(db)
    still_held = {h.ref for lane in lanes.values() if lane.key not in rejected for h in lane.holds}
    for key in rejected:
        lane = lanes[key]
        for hold in lane.holds:
            if hold.kind == "changeset":
                stored = changesets.get(hold.ref)
                if stored is not None and stored.state in ("proposed", "conflicted"):
                    changesets.set_state(hold.ref, "rejected", resolved_by="review")
                    emit(
                        "changeset.rejected",
                        stored.changeset.step_id,
                        {"changeset_id": hold.ref},
                    )
            elif hold.kind == "gate" and hold.ref != gate.id and hold.ref not in still_held:
                gates.cancel(hold.ref)
        lane.state, lane.position, lane.holds = "rejected", None, []
        emit("lane.finished", None, lane_json(lane))
    return RejectOutcome(settle(db, run_id, emit), rejected)


def settle(db: AppDB, run_id: str, emit: EmitRun) -> Settled:
    """After lanes of a waiting run ended outside the engine: requeue, keep waiting, or
    finish it (LANES-7, -8, -9)."""
    if db.requeue_if_released(run_id):
        return "requeued"
    lanes = AppLanes(db).load(run_id)
    if any(lane.state in ("waiting", "active") for lane in lanes.values()):
        return "waiting"
    states = {lane.state for lane in lanes.values()}
    assert states <= FINAL_LANE_STATES
    rejected = states == {"rejected"}
    status = "cancelled" if rejected else "failed" if "failed" in states else "succeeded"
    error = REJECTED_AT_REVIEW if rejected else None
    if not db.finish_waiting(run_id, status, error):
        return "not_waiting"
    _close_leftovers(db, run_id, emit)
    data: dict[str, JsonValue] = {"status": status}
    if rejected:
        data["reason"] = REJECTED_AT_REVIEW
    emit("run.finished", None, data)
    return "finished"


def _close_leftovers(db: AppDB, run_id: str, emit: EmitRun) -> None:
    """A finished run leaves nothing pending: proposed ChangeSets are rejected and
    pending gates and import decisions cancelled (CHANGESET-7, REVIEW-10, IMPORT-UI-14)."""
    store = AppChangeSets(db)
    for stored in store.for_run(run_id, "proposed"):
        store.set_state(stored.changeset.id, "rejected", resolved_by="review")
        emit("changeset.rejected", stored.changeset.step_id, {"changeset_id": stored.changeset.id})
    AppReviewGates(db).cancel_for_run(run_id)
    AppImportDecisions(db).cancel_for_run(run_id)


def _finish_whole(db: AppDB, run_id: str, emit: EmitRun) -> bool:
    if not db.cancel_waiting(run_id, REJECTED_AT_REVIEW):
        return False
    _close_leftovers(db, run_id, emit)
    emit("run.finished", None, {"status": "cancelled", "reason": REJECTED_AT_REVIEW})
    return True


def cancel_lanes(db: AppDB, run_id: str, emit: EmitRun) -> None:
    """A waiting run was cancelled: its open lanes end ``cancelled`` (LANES-13)."""
    store = AppLanes(db)
    lanes = store.load(run_id)
    open_keys = [k for k, lane in lanes.items() if not lane.final]
    for key in store.set_state(run_id, open_keys, "cancelled", only_from=("waiting", "active")):
        lane = lanes[key]
        lane.state, lane.position, lane.holds = "cancelled", None, []
        emit("lane.finished", None, lane_json(lane))
