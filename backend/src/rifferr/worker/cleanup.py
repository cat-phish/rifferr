"""Server-side helpers for the ``files.cleanup`` step (S-FILES-CLEANUP)."""

from __future__ import annotations

from pathlib import Path

from rifferr.appdb import AppDB, AppImportDecisions
from rifferr.core.files import BusyFolder
from rifferr.core.profile import Profile
from rifferr.core.triggers import watch_path
from rifferr.core.workflow import (
    WORKFLOW_SUFFIXES,
    InterpolationError,
    WorkflowLoadError,
    load_workflow,
)
from rifferr.profile_store import WORKFLOWS_DIR

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


def watched_folders(profile: Profile) -> list[Path]:
    """Watch paths of every workflow in the profile, armed or not (FILES-CLEANUP-18).
    A workflow that doesn't load or whose path doesn't resolve is left out."""
    folder = profile.root / WORKFLOWS_DIR
    out: list[Path] = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.iterdir()):
        if not f.is_file() or f.suffix not in WORKFLOW_SUFFIXES:
            continue
        try:
            wf, _ = load_workflow(f.read_text(encoding="utf-8"), f.name)
            path = watch_path(wf, profile.vars, profile.name)
        except (OSError, UnicodeDecodeError, WorkflowLoadError, InterpolationError):
            continue
        if path is not None and Path(path).is_absolute():
            out.append(Path(path))
    return out
