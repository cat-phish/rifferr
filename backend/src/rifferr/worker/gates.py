"""Ending a run rejected at a review gate (REVIEW-8), shared by server and worker.

The server does it directly for ``on_reject: stop``; the worker does it after the
undo for ``on_reject: undo``.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import JsonValue

from rifferr.appdb import AppChangeSets, AppDB, AppReviewGates
from rifferr.core.review import REJECTED_AT_REVIEW

#: (event type, step id, data) -> None; puts an event on the run's stream.
EmitRun = Callable[[str, str | None, dict[str, JsonValue]], None]


def finish_rejected(db: AppDB, run_id: str, emit: EmitRun) -> bool:
    """End a waiting run as ``cancelled`` with reason ``rejected_at_review``.

    Its proposed ChangeSets become rejected and any other pending gate is cancelled.
    False (and nothing done) if the run isn't waiting any more, e.g. it was cancelled
    meanwhile.
    """
    if not db.cancel_waiting(run_id, REJECTED_AT_REVIEW):
        return False
    store = AppChangeSets(db)
    for stored in store.for_run(run_id, "proposed"):
        store.set_state(stored.changeset.id, "rejected", resolved_by="review")
        emit("changeset.rejected", stored.changeset.step_id, {"changeset_id": stored.changeset.id})
    AppReviewGates(db).cancel_for_run(run_id)
    emit("run.finished", None, {"status": "cancelled", "reason": REJECTED_AT_REVIEW})
    return True
