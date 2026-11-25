"""The ``files.cleanup`` step (S-FILES-CLEANUP): list what is left in a folder and, when
asked, delete what the workflow's patterns name.

It never leaves its folder, never follows symbolic links, never touches library files,
music the backend could import (unless ``allow_audio``), or folders other work still
needs. ``report`` mode (the default) changes nothing; ``delete`` runs only in trusted
workflows, and ``review: true`` asks a person first.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.duration import Duration, parse_duration
from rifferr.core.files import BusyFolder, CleanupDecision, CleanupEntry, CleanupOptions
from rifferr.engine import StepContext, StepResult
from rifferr.engine.run import Scalar
from rifferr.engine.steps import CleanupRequest, StepFailure, StepRefused
from rifferr.steps.cleanup_plan import (
    CleanupError,
    Outcome,
    Plan,
    check_root,
    check_trash,
    execute,
    plan,
    purge_trash,
)

MAX_LISTED: Final = 1000
"""Entries in the report event and a cleanup decision (FILES-CLEANUP-8)."""
GLOB_HELP: Final = (
    "Glob patterns on paths relative to path; without '/' they match a file name at any "
    "depth (*.nfo), with '/' the relative path (Scans/*.jpg); ** spans folders; no case"
)


class CleanupParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="The folder to clean, e.g. {{ run.input }}")
    mode: Literal["report", "delete"] = Field(
        default="report", description="report: list only; delete: delete what delete names"
    )
    delete: list[str] = Field(
        default_factory=list[str], description=f"Files to delete. {GLOB_HELP}"
    )
    keep: list[str] = Field(
        default_factory=list[str], description="Files never deleted, checked first (globs)"
    )
    empty_dirs: bool = Field(default=True, description="Remove folders left empty")
    remove_root: bool = Field(default=False, description="Also remove path when it ends empty")
    trash: str | None = Field(
        default=None, description="Move files to this folder instead of deleting them"
    )
    review: bool = Field(
        default=False, description="Ask in the review queue before deleting (delete mode)"
    )
    timeout: Duration | None = Field(
        default=None, description="With review: decide automatically after this long"
    )
    on_timeout: Literal["keep", "delete"] | None = Field(
        default=None, description="The decision once timeout passes (default keep)"
    )
    allow_audio: bool = Field(
        default=False,
        description="Let delete patterns match music the backend could import",
    )

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.mode == "delete" and not self.delete and not self.empty_dirs:
            raise ValueError("mode delete needs delete patterns or empty_dirs: nothing to do")
        if self.review and self.mode != "delete":
            raise ValueError("review only applies to mode delete")
        if self.on_timeout is not None and self.timeout is None:
            raise ValueError("on_timeout needs a timeout")
        if self.timeout is not None and not self.review:
            raise ValueError("timeout only applies with review: true")
        return self

    def options(self) -> CleanupOptions:
        return CleanupOptions(
            delete=self.delete,
            keep=self.keep,
            empty_dirs=self.empty_dirs,
            remove_root=self.remove_root,
            trash=self.trash,
            allow_audio=self.allow_audio,
        )


class FilesCleanupStep:
    name = "files.cleanup"
    waits = "always"  # may put items in the review queue (UI-REFRESH-13)
    params_model = CleanupParams
    requires = BackendCapabilities(files=True)
    run_level = True  # FILES-CLEANUP-2: about a folder, not about albums
    accepts_when = False

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, CleanupParams)
        if params.mode == "delete" and not ctx.trusted:  # FILES-CLEANUP-13
            raise StepRefused(
                "files.cleanup in delete mode runs only in trusted workflows; "
                "trust the workflow, or use mode: report"
            )
        try:
            root = check_root(Path(params.path), ctx.backend.library_dirs(), Path.home())
            trash = check_trash(Path(params.trash), root) if params.trash else None
        except CleanupError as exc:
            raise StepFailure(str(exc)) from None
        if trash is not None:
            self._purge(ctx, trash)
        try:
            listing = build_plan(ctx, root, params.options())
        except CleanupError as exc:
            raise StepFailure(str(exc)) from None
        report(ctx, listing)
        outputs = counts(listing)
        if params.mode == "report":
            return StepResult(outputs=outputs)
        if params.review:
            decision = ctx.request_cleanup(
                CleanupRequest(
                    path=str(root),
                    entries=listing.entries[:MAX_LISTED],
                    truncated=max(0, len(listing.entries) - MAX_LISTED),
                    totals=listing.totals(),
                    options=params.options(),
                    timeout=parse_duration(params.timeout) if params.timeout else None,
                    on_timeout=params.on_timeout or "keep",
                )
            )
            ctx.log(f"waiting for review before cleaning {root}")
            return StepResult(outputs={**outputs, "decision": decision})
        outcome = execute(listing, trash=trash, run_id=ctx.run_id)
        log_outcome(ctx, outcome)
        if outcome.failed:
            raise StepFailure(f"{outcome.failed} file(s) couldn't be removed; see the log")
        return StepResult(outputs={**outputs, **outcome_counts(outcome)})

    @staticmethod
    def _purge(ctx: StepContext, trash: Path) -> None:
        settings = ctx.profile_settings
        keep = settings.files.trash_keep if settings is not None else None
        if keep is None:
            return
        from datetime import UTC, datetime

        removed, others = purge_trash(trash, parse_duration(keep), datetime.now(UTC))
        for run_id in removed:
            ctx.log(f"trash: emptied run folder {run_id} (older than {keep})")
        if others:
            ctx.log(f"trash: left alone (not run folders): {', '.join(others)}")


def build_plan(ctx: StepContext, root: Path, options: CleanupOptions) -> Plan:
    backend = ctx.backend
    return plan(
        root,
        delete=options.delete,
        keep=options.keep,
        empty_dirs=options.empty_dirs,
        remove_root=options.remove_root,
        busy=ctx.busy_folders(),
        library_files=backend.library_files,
        importable=backend.importable,
        allow_audio=options.allow_audio,
        watched=ctx.watched_folders(),
    )


def report(ctx: StepContext, listing: Plan) -> None:
    """FILES-CLEANUP-8, -11: one log line per entry and the report event."""
    left = 0
    for entry in listing.entries:
        stream = "info"
        if entry.cls == "left":
            left += 1
            stream = "error"
        ctx.log(_line(entry), stream=stream)
    for skipped in listing.skipped:
        ctx.log(f"skipped {skipped}", stream="error")
    if left:
        ctx.log(f"{left} file(s) left that no pattern deletes; check them by hand", "error")
    entries: list[JsonValue] = [
        e.model_dump(mode="json", exclude={"mtime_ns"}) for e in listing.entries
    ]
    ctx.emit(
        "run.step.cleanup.report",
        path=str(listing.root),
        entries=entries[:MAX_LISTED],
        truncated=max(0, len(entries) - MAX_LISTED),
        totals=cast(dict[str, JsonValue], dict(listing.totals())),
    )


def _line(entry: CleanupEntry) -> str:
    size = f" ({entry.size} bytes)" if entry.size is not None else ""
    reason = f" [{entry.reason}]" if entry.reason else ""
    suffix = "/" if entry.kind == "dir" else ""
    return f"{entry.cls}: {entry.path}{suffix}{size}{reason}"


def counts(listing: Plan) -> dict[str, Scalar]:
    totals = listing.totals()
    return {
        "left": totals.get("left", 0),
        "in_library": totals.get("in_library", 0),
        "busy": totals.get("busy", 0),
        "to_delete": totals.get("delete", 0),
        "empty_dirs": totals.get("empty", 0),
    }


def outcome_counts(outcome: Outcome) -> dict[str, Scalar]:
    return {
        "deleted": outcome.deleted,
        "moved": outcome.moved,
        "dirs_removed": outcome.dirs_removed,
        "failed": outcome.failed,
        "changed": outcome.changed,
        "bytes_freed": outcome.bytes_freed,
    }


def log_outcome(ctx: StepContext, outcome: Outcome) -> None:
    for stream, line in outcome.lines:
        ctx.log(line, stream=stream)


def apply_reviewed(
    decision: CleanupDecision,
    backend: LibraryBackend,
    busy: list[BusyFolder],
    run_id: str,
    watched: Sequence[Path] = (),
) -> Outcome:
    """Clean what a person reviewed and chose Delete for (FILES-CLEANUP-14).

    The folder is listed again, so library files, busy folders and importable music are
    checked as of now; only entries the person saw as ``delete`` or ``empty`` and that
    are still that are removed, and a file whose size or time changed since the listing
    is left. Raises ``CleanupError`` when the folder or trash is not safe any more.
    """
    options = decision.options
    root = check_root(Path(decision.path), backend.library_dirs(), Path.home())
    trash = check_trash(Path(options.trash), root) if options.trash else None
    fresh = plan(
        root,
        delete=options.delete,
        keep=options.keep,
        empty_dirs=options.empty_dirs,
        remove_root=options.remove_root,
        busy=busy,
        library_files=backend.library_files,
        importable=backend.importable,
        allow_audio=options.allow_audio,
        watched=watched,
    )
    reviewed = {e.path: e for e in decision.entries if e.cls in ("delete", "empty")}
    entries = [
        reviewed[e.path] if e.path in reviewed and reviewed[e.path].cls == e.cls else e
        for e in fresh.entries
    ]
    outcome = execute(
        Plan(root, entries, fresh.skipped), trash=trash, run_id=run_id, only=set(reviewed)
    )
    if decision.truncated:
        outcome.lines.append(
            ("info", f"{decision.truncated} entries weren't shown for review and were left")
        )
    return outcome
