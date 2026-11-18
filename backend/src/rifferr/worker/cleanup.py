"""Server-side helpers for the ``files.cleanup`` step (S-FILES-CLEANUP)."""

from __future__ import annotations

from pathlib import Path

from rifferr.appdb import AppDB, AppImportDecisions
from rifferr.core.files import BusyFolder

#: Run states whose input another cleanup must leave alone (FILES-CLEANUP-7).
ACTIVE_RUN_STATES = ("queued", "running", "waiting_review")


def busy_folders(db: AppDB, profile: str, run_id: str | None) -> list[BusyFolder]:
    """Folders other work still needs (FILES-CLEANUP-7): those of the profile's pending
    import decisions, and the inputs of other queued, running or waiting runs (any
    profile: two profiles can share an inbox). ``run_id``'s own input is not busy."""
    busy: list[BusyFolder] = []
    for decision in AppImportDecisions(db).pending(profile=profile):
        busy.append(BusyFolder(Path(decision.path), "pending import decision"))
    for state in ACTIVE_RUN_STATES:
        for job in db.jobs(state=state):
            if job.kind != "run" or job.run_id == run_id:
                continue
            value = job.payload.get("input")
            if isinstance(value, str) and value:
                busy.append(BusyFolder(Path(value), f"input of run {job.run_id}"))
    return busy
