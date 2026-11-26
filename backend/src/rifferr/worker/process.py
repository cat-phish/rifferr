"""The worker process: one profile, one backend, one run at a time (F-WORKER).

stdin carries ``WorkerCommand`` lines from the server, stdout carries
``WorkerMessage`` lines back (WORKER-9). The real stdout is moved to a private
file descriptor at start-up and fd 1 is pointed at stderr, so anything else that
prints (steps, beets, plugins) can't corrupt the channel.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import shlex
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from pydantic import JsonValue, ValidationError

from rifferr.appdb import (
    HOLDING_CHANGESETS,
    AppChangeSets,
    AppChoiceReviews,
    AppCleanupDecisions,
    AppCoverReviews,
    AppDB,
    AppImportDecisions,
    AppJournal,
    AppLanes,
    AppMoveConflicts,
    AppPruneDecisions,
    AppRemovalJournal,
    AppReviewGates,
    Job,
    JobState,
)
from rifferr.backends.base import CapabilityError, LibraryBackend, QueryError, TemplateError
from rifferr.backends.loader import load_backend
from rifferr.choice_rules import (
    append_rule,
    load_rules,
    query_literal,
    resolve_rules,
    rule_values,
)
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.choices import CUSTOM, ChoiceReview
from rifferr.core.covers import ART_FIELD
from rifferr.core.ids import new_ulid
from rifferr.core.imports import (
    ImportAction,
    ImportApplyError,
    ImportDecision,
    merge_candidates,
)
from rifferr.core.moves import MoveSettings
from rifferr.core.profile import Profile, ProfileError, ProfileLoadError, Redactor
from rifferr.core.review import ReviewGate
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, label
from rifferr.core.workflow import WorkflowLoadError, load_workflow
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import (
    Engine,
    Event,
    FailedTarget,
    PassRecord,
    Run,
    RunState,
    RunValidationError,
    SecretAccess,
    StepRecord,
    StepTypeRegistry,
)
from rifferr.engine.changesets import (
    ApplyOutcome,
    SnapshotIndex,
    apply_changeset,
    build_changesets,
    current_values,
    file_changeset,
    new_changeset,
    proposed_event,
    reject_changeset,
    repropose_targets,
)
from rifferr.engine.choices import decision_actions
from rifferr.engine.events import profile_stream
from rifferr.engine.lanes import lane_json
from rifferr.engine.recovery import recover_pending
from rifferr.engine.steps import NotifyTargets
from rifferr.engine.undo import UndoContext, UndoError, undo_entry, undo_run
from rifferr.notify import ProfileTargets, default_notifier
from rifferr.profile_store import app_db_path, config_root, load_profile
from rifferr.steps.cleanup_plan import CleanupError
from rifferr.steps.files_cleanup import apply_reviewed, outcome_counts
from rifferr.steps.library_prune import drop_albums, file_exists
from rifferr.steps.registry import load_step_types
from rifferr.steps.tagscript import proposal as tagscript_proposal
from rifferr.tagscript.evaluator import run_script
from rifferr.tagscript.models import TagScript
from rifferr.worker.cleanup import busy_folders, watched_folders
from rifferr.worker.gates import cancel_pending_items, gate_albums, reject_gate_lanes, settle
from rifferr.worker.import_undo import recover_removals, undo_import
from rifferr.worker.protocol import HEARTBEAT_INTERVAL, WorkerCommand, WorkerMessage, encode
from rifferr.workflow_calls import stored_calls

log = logging.getLogger("rifferr.worker")

Send = Callable[[WorkerMessage], None]
Commands = asyncio.Queue[WorkerCommand | None]


class Channel:
    """Thread-safe writer for worker -> server messages."""

    def __init__(self, out: BinaryIO) -> None:
        self._out = out
        self._lock = threading.Lock()
        self.closed = False

    def send(self, message: WorkerMessage) -> None:
        data = encode(message)
        with self._lock:
            if self.closed:
                return
            try:
                self._out.write(data)
                self._out.flush()
            except (BrokenPipeError, ValueError):
                self.closed = True  # server gone; stdin EOF will stop us


#: ChangeSet states that still hold a suspended run (CHANGESET-7).
HOLDING = frozenset(HOLDING_CHANGESETS)


class _ForwardSink:
    """Engine event sink that hands events to the server, which persists them (WORKER-4)."""

    def __init__(
        self,
        send: Send,
        sent: dict[str, int] | None = None,
        before_send: Callable[[Event], None] | None = None,
    ) -> None:
        self._send = send
        self.sent = sent if sent is not None else {}
        """run id -> last seq sent, for events the server may not have stored yet."""
        self._before_send = before_send

    def append(self, event: Event) -> None:
        if self._before_send is not None:
            self._before_send(event)
        self.sent[event.run_id] = max(self.sent.get(event.run_id, 0), event.seq)
        self._send(WorkerMessage(kind="event", event=event))

    def since(self, run_id: str, seq: int) -> Iterable[Event]:
        return []


class Worker:
    def __init__(
        self,
        profile: Profile,
        db: AppDB,
        registry: StepTypeRegistry,
        backend: LibraryBackend,
        send: Send,
        *,
        secrets: SecretAccess | None = None,
        idle_timeout: float | None = None,
        cancel_grace: float = 5.0,
        notify: NotifyTargets | None = None,
    ) -> None:
        self.profile = profile
        self.db = db
        self.registry = registry
        self.backend = backend
        self.send = send
        self.secrets = secrets
        self.notify = notify
        """The profile's notification targets for ``notify`` steps (S-NOTIFY)."""
        self.idle_timeout = idle_timeout
        self.cancel_grace = cancel_grace
        self._engine: Engine | None = None
        self._run: Run | None = None
        self._sent: dict[str, int] = {}
        self.journal = AppJournal(db)
        self.changesets = AppChangeSets(db)
        self.gates = AppReviewGates(db)
        self.lanes = AppLanes(db)
        self.imports = AppImportDecisions(db)
        self.cleanups = AppCleanupDecisions(db)
        self.covers = AppCoverReviews(db)
        self.choices = AppChoiceReviews(db)
        self.moves = AppMoveConflicts(db)
        self.prunes = AppPruneDecisions(db)

    async def serve(self, commands: Commands) -> None:
        """Run jobs until stopped, the server goes away, or idle too long (WORKER-7)."""
        loop = asyncio.get_running_loop()
        self.send(WorkerMessage(kind="ready"))
        self.recover()
        current: asyncio.Task[None] | None = None
        stopping = False
        idle_since = loop.time()
        get: asyncio.Future[WorkerCommand | None] = asyncio.ensure_future(commands.get())
        try:
            while True:
                if current is None and not stopping:
                    job = self.db.claim_next(self.profile.name)
                    if job is not None:
                        current = asyncio.create_task(self._run_job(job))
                if current is None and stopping:
                    return
                timeout = None
                if current is None and self.idle_timeout is not None:
                    timeout = max(0.0, idle_since + self.idle_timeout - loop.time())
                waiting: set[asyncio.Future[object]] = {get}  # type: ignore[arg-type]
                if current is not None:
                    waiting.add(current)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(
                    waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    log.info("idle for %s s, exiting", self.idle_timeout)
                    return
                if current is not None and current in done:
                    current = None
                    idle_since = loop.time()
                if get in done:
                    command = get.result()
                    get = asyncio.ensure_future(commands.get())
                    stopping = self._handle(command) or stopping
        finally:
            get.cancel()
            if current is not None:
                current.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await current

    def _handle(self, command: WorkerCommand | None) -> bool:
        """Apply a command; return True when the worker must stop."""
        if command is None:  # stdin closed: the server is gone
            self._cancel_current()
            return True
        if command.profile != self.profile.name:
            self.send(
                WorkerMessage(
                    kind="error",
                    message=f"worker for profile '{self.profile.name}' refused a "
                    f"'{command.kind}' command for profile '{command.profile}'",
                )
            )
            return False
        if command.kind == "stop":
            self._cancel_current()
            return True
        if (
            command.kind == "cancel_run"
            and self._run is not None
            and command.run_id == self._run.id
        ):
            self._cancel_current()
        return False  # start_run: the loop claims queued jobs itself

    def _cancel_current(self) -> None:
        if self._engine is not None and self._run is not None:
            self._engine.cancel(self._run.id)

    def recover(self) -> None:
        """Settle applies a previous worker left pending (JOURNAL-3). Never writes tags."""
        try:
            settled = recover_pending(
                self.profile.name,
                journal=self.journal,
                backend=self.backend,
                store=self.changesets,
                emitter_for=self._changeset_emitter,
                now=lambda: datetime.now(UTC),
            )
        except Exception:  # a broken library must not stop the worker from serving
            log.exception("journal recovery failed")
            return
        for entry in settled:
            note = " (uncertain: needs review)" if entry.uncertain else ""
            log.warning("recovered journal entry %s: %s%s", entry.id, entry.state, note)
        if self.backend.capabilities.remove:
            try:
                for removal in recover_removals(self.db, self.backend, self.profile.name):
                    log.warning("settled interrupted album removal %s", removal)
            except Exception:
                log.exception("album removal recovery failed")

    def _current_settings(self) -> Profile:
        """``profile.yaml`` as it is now, so a run uses settings saved since the worker
        started, such as cover weights (F-COVER-PICKER open question 2). Falls back to
        the profile the worker started with when the file can't be read."""
        try:
            return load_profile(self.profile.root.parent.parent, self.profile.name)
        except (ProfileError, ProfileLoadError, OSError) as exc:
            log.warning(
                "profile %s: using the settings loaded at start: %s", self.profile.name, exc
            )
            return self.profile

    def _next_seq(self, run_id: str) -> int:
        return max(self.db.last_seq(run_id), self._sent.get(run_id, 0)) + 1

    async def _run_job(self, job: Job) -> None:
        if job.kind == "review_decision":
            self._decide(job)
            return
        if job.kind == "undo":
            self._undo(job)
            return
        if job.kind == "review_reject":
            self._reject_at_review(job)
            return
        if job.kind == "gate_apply":
            self._apply_gate_edits(job)
            return
        if job.kind == "manual_apply":
            self._apply_manual(job)
            return
        if job.kind == "repropose":
            self._repropose(job)
            return
        if job.kind == "import_decision":
            self._apply_import(job)
            return
        if job.kind == "import_lookup":
            self._lookup_import(job)
            return
        if job.kind == "cleanup_apply":
            self._apply_cleanup(job)
            return
        if job.kind == "import_undo":
            self._undo_import(job)
            return
        if job.kind == "cover_apply":
            self._apply_cover(job)
            return
        if job.kind == "choice_apply":
            self._apply_choice(job)
            return
        if job.kind == "move_retry":
            self._retry_move(job)
            return
        if job.kind == "prune_apply":
            self._apply_prune(job)
            return
        payload = job.payload
        source = str(payload.get("source", ""))
        source_name = str(payload.get("source_name", "<workflow>"))
        run_vars = payload.get("vars") or {}
        assert isinstance(run_vars, dict)
        resume_after = payload.get("resume_after")
        assert resume_after is None or isinstance(resume_after, str)
        engine = Engine(
            self.registry,
            _ForwardSink(self.send, self._sent, self._record_run_state),
            new_id=lambda: job.run,
            cancel_grace=self.cancel_grace,
            journal=self.journal,
            changesets=self.changesets,
            gates=self.gates,
            lanes=self.lanes,
            imports=self.imports,
            cleanups=self.cleanups,
            covers=self.covers,
            choices=self.choices,
            moves=self.moves,
            prunes=self.prunes,
            removals=AppRemovalJournal(self.db, self.profile.name),
        )
        try:
            wf, doc = load_workflow(source, source_name)
            run = engine.create_run(
                wf,
                source.encode(),
                profile_name=self.profile.name,
                profile_vars=self.profile.vars,
                backend=self.backend,
                run_vars={str(k): str(v) for k, v in run_vars.items()},
                run_input=str(payload.get("input") or ""),
                trigger=str(payload.get("trigger") or "manual"),
                doc=doc,
                redactor=Redactor([]),
                trusted=payload.get("trusted") is True,
                work_dir=self.profile.root,
                profile_dir=self.profile.root,
                secrets=self.secrets,
                notify=self.notify,
                profile_settings=self._current_settings(),
                busy=lambda: busy_folders(self.db, self.profile.name, job.run),
                watched=lambda: watched_folders(self.profile),
                imported=lambda path: self._supersede_imports(path, job.run),
                calls=stored_calls(wf, doc, payload),
                last_seq=self._next_seq(job.run) - 1 if resume_after else 0,
            )
            if resume_after is not None:
                engine.restore_steps(run, step_records(self.db.events_since(job.run, 0)))
        except (WorkflowLoadError, RunValidationError) as exc:
            self._fail_unstarted(job, str(exc))
            return
        except Exception as exc:  # a bug (e.g. in a step's parameter model)
            log.exception("run %s could not be created", job.run)
            self._fail_unstarted(job, f"internal error: {type(exc).__name__}: {exc}")
            return
        self._engine, self._run = engine, run
        try:
            await engine.execute(run, resume_after=resume_after)
            # Normally already done by _record_run_state; this covers anything missed.
            if run.state is RunState.waiting_review:
                assert run.suspended_after is not None
                self.db.suspend(job.run, run.suspended_after, run.holds, run.gates)
            else:
                self.db.finish(job.run, run.state.value)  # type: ignore[arg-type]
                cancel_pending_items(self.db, job.run)
        except Exception as exc:  # an engine bug: still end the run cleanly
            log.exception("run %s crashed", run.id)
            self.db.finish(job.run, "failed", f"{type(exc).__name__}: {exc}")
            cancel_pending_items(self.db, job.run)
            if not run.finished:
                self._send_finished(run.id, run.last_seq + 1, f"internal error: {exc}")
        finally:
            self._engine = self._run = None

    def _record_run_state(self, event: Event) -> None:
        """Store the job's new state before the server hears about it, so anyone who sees
        ``run.finished`` or ``run.waiting_review`` also sees the job row agree."""
        if event.type == "run.finished":
            status = event.data.get("status")
            if status in ("succeeded", "failed", "cancelled"):
                self.db.finish(event.run_id, status)  # type: ignore[arg-type]
        elif event.type == "run.waiting_review":
            after, holds = event.data.get("after"), event.data.get("changesets")
            gates = event.data.get("gates") or []
            if isinstance(after, str) and isinstance(holds, list) and isinstance(gates, list):
                self.db.suspend(
                    event.run_id, after, [str(h) for h in holds], [str(g) for g in gates]
                )

    def _fail_unstarted(self, job: Job, error: str) -> None:
        self.db.finish(job.run, "failed", error)
        self._send_finished(job.run, self._next_seq(job.run), error)

    # -- review decisions (CHANGESET-7, -9, -13) -----------------------------------

    def _decide(self, job: Job) -> None:
        """Apply or reject one ChangeSet, then resume its run once nothing holds it."""
        payload = job.payload
        changeset_id = str(payload.get("changeset_id"))
        stored = self.changesets.get(changeset_id)
        if stored is None:
            self._finish_job(job, "failed", f"no ChangeSet {changeset_id}")
            return
        cs = stored.changeset
        by = payload.get("by")
        resolved_by = by if isinstance(by, str) else None
        accepted_raw = payload.get("accepted")
        accepted = (
            [int(i) for i in accepted_raw if isinstance(i, int)]  # pyright: ignore[reportUnknownVariableType]
            if isinstance(accepted_raw, list)
            else None
        )
        emit = self._changeset_emitter(cs.run_id, cs.step_id)
        try:
            if stored.state not in HOLDING:
                raise ValueError(f"ChangeSet {changeset_id} is already {stored.state}")
            if payload.get("decision") == "reject":
                reject_changeset(
                    changeset_id, store=self.changesets, emit=emit, resolved_by=resolved_by
                )
            elif stored.state == "conflicted":
                raise ValueError(
                    f"ChangeSet {changeset_id} conflicted; reject it or propose it again"
                )
            else:
                if cs.replan is not None:  # LIBRARY-MOVE-19: move what the album is now
                    cs, problems = self._refresh_move(cs)
                    if problems:
                        raise ValueError("the move conflicts now: " + "; ".join(problems))
                outcome = apply_changeset(
                    cs,
                    backend=self.backend,
                    journal=self.journal,
                    store=self.changesets,
                    new_id=new_ulid,
                    now=lambda: datetime.now(UTC),
                    emit=emit,
                    accepted=accepted,
                    resolved_by=resolved_by,
                )
                for failure in outcome.entry.write_failures if outcome.entry else []:
                    emit(
                        "run.step.log",
                        {
                            "stream": "error",
                            "line": f"{failure.path}: tags not written: {failure.error}",
                            "target": failure.target.id,
                        },
                    )
                if cs.files and outcome.error is not None:  # LIBRARY-MOVE-10, -19
                    self._fail_move_lane(cs, outcome.error)
                    self._finish_job(job, "failed", outcome.error)
                    if cs.run_id is not None:
                        run_id = cs.run_id
                        self.lanes.resolve_hold(run_id, cs.id)
                        settle(
                            self.db,
                            run_id,
                            lambda type, step, d: self._changeset_emitter(run_id, step)(type, d),
                        )
                    return
        except Exception as exc:
            log.warning("review decision on %s failed: %s", changeset_id, exc)
            self._finish_job(job, "failed", f"{type(exc).__name__}: {exc}")
            return
        note = payload.get("note")
        if isinstance(note, str):
            self.changesets.set_note(changeset_id, note)
        self._finish_job(job, "succeeded")
        if cs.run_id is not None:
            self._resume_if_released(cs.run_id)

    # -- import decisions (F-IMPORT-UI) -----------------------------------------------

    def _apply_import(self, job: Job) -> None:
        """Import a decided folder, then let its lane go on (IMPORT-UI-11 .. -13)."""
        payload = job.payload
        decision_id = str(payload.get("decision_id"))
        decision = self.imports.get(decision_id)
        if decision is None:
            self._finish_job(job, "failed", f"no import decision {decision_id}")
            return
        emit = self._changeset_emitter(decision.run_id, decision.step_id)
        action = str(payload.get("action"))
        candidate_id = payload.get("candidate_id")
        if decision.kind == "duplicate" and decision.candidates:
            candidate_id = decision.candidates[0].id  # the match the duplicate was found for
        by, note = payload.get("by"), payload.get("note")
        try:
            result = self.backend.import_decided(
                Path(decision.path),
                cast(ImportAction, action),
                fingerprint=decision.fingerprint,
                candidate_id=candidate_id if isinstance(candidate_id, str) else None,
                run_id=decision.run_id,
                options=decision.options,
                set_fields=decision.set_fields,
            )
        except Exception as exc:  # IMPORT-UI-13: stays pending, with the error
            message = (
                str(exc) if isinstance(exc, ImportApplyError) else f"{type(exc).__name__}: {exc}"
            )
            decision.error = message
            self.imports.release(decision)
            emit(
                "run.step.log",
                {"stream": "error", "line": f"import decision for {decision.path}: {message}"},
            )
            self._finish_job(job, "failed", message)
            return
        album = result.imported[0] if result.imported else None
        self._resolve_import(
            decision,
            cast(ImportAction, action),
            candidate_id=candidate_id if isinstance(candidate_id, str) else None,
            by=by if isinstance(by, str) else None,
            note=note if isinstance(note, str) else None,
            album=album,
        )
        self._finish_job(job, "succeeded", result={"album_id": decision.album_id})
        self._settle(decision.run_id)
        if album is not None:
            self._supersede_imports(decision.path, decision.run_id)

    def _resolve_import(
        self,
        decision: ImportDecision,
        action: ImportAction,
        *,
        candidate_id: str | None,
        by: str | None,
        note: str | None,
        album: AlbumSnapshot | None,
    ) -> None:
        """Record a decided import and end or rename its lane (IMPORT-UI-12, -17)."""
        emit = self._changeset_emitter(decision.run_id, decision.step_id)
        decision.state = "skipped" if action == "skip" else "applied"
        decision.action = action
        decision.candidate_id = candidate_id
        decision.error = None
        decision.decided_by = by
        decision.decided_at = datetime.now(UTC)
        decision.note = note
        decision.album_id = album.id if album is not None else None
        self.imports.release(decision)
        data: dict[str, JsonValue] = {"decision_id": decision.id, "action": action}
        if album is not None:
            data["album_id"] = album.id
        emit("import.resolved", data)
        key = decision.lane_key
        if album is None:  # IMPORT-UI-17: a skipped album ends its lane skipped
            lanes = self.lanes.load(decision.run_id)
            for moved in self.lanes.set_state(
                decision.run_id, [key], "skipped", only_from=("waiting", "active")
            ):
                lane = lanes[moved]
                lane.state, lane.position, lane.holds = "skipped", None, []
                emit("lane.finished", lane_json(lane))
        else:
            self.lanes.rename(decision.run_id, key, album.id, label(album))  # LANES-14
            self.lanes.resolve_hold(decision.run_id, decision.id)

    def _settle(self, run_id: str) -> None:
        settle(
            self.db,
            run_id,
            lambda type, step, d: self._changeset_emitter(run_id, step)(type, d),
        )

    def _supersede_imports(self, path: str, run_id: str) -> None:
        """IMPORT-UI-19: ``run_id`` imported the folder ``path``, so other runs' pending,
        unclaimed decisions for that same folder are stale. Skip them."""
        folder = Path(path)
        for old in self.imports.pending(profile=self.profile.name):
            if old.run_id == run_id or Path(old.path) != folder:
                continue
            if self.imports.claimed(old.id):  # a person is answering it right now
                continue
            self._resolve_import(
                old,
                "skip",
                candidate_id=None,
                by="rifferr",
                note=f"superseded: run {run_id} imported this folder",
                album=None,
            )
            self._settle(old.run_id)

    def _apply_cleanup(self, job: Job) -> None:
        """Clean a folder a person (or the timeout) chose Delete for (FILES-CLEANUP-14)."""
        payload = job.payload
        decision_id = str(payload.get("decision_id"))
        decision = self.cleanups.get(decision_id)
        if decision is None:
            self._finish_job(job, "failed", f"no cleanup decision {decision_id}")
            return
        emit = self._changeset_emitter(decision.run_id, decision.step_id)
        by, note = payload.get("by"), payload.get("note")
        try:
            outcome = apply_reviewed(
                decision,
                self.backend,
                busy_folders(self.db, self.profile.name, decision.run_id),
                decision.run_id,
                watched_folders(self.profile),
            )
        except Exception as exc:  # the folder isn't safe any more: nothing was removed
            message = str(exc) if isinstance(exc, CleanupError) else f"{type(exc).__name__}: {exc}"
            emit(
                "run.step.log",
                {"stream": "error", "line": f"cleanup of {decision.path}: {message}"},
            )
            result: dict[str, int] = {"failed": 1}
        else:
            for stream, line in outcome.lines:
                emit("run.step.log", {"stream": stream, "line": line})
            result = {k: int(v) for k, v in outcome_counts(outcome).items()}
        decision.state = "deleted"
        decision.decided_by = by if isinstance(by, str) else None
        decision.decided_at = datetime.now(UTC)
        decision.note = note if isinstance(note, str) else None
        decision.result = result
        self.cleanups.release(decision)
        emit(
            "cleanup.resolved",
            {
                "decision_id": decision.id,
                "decision": "delete",
                "decided_by": decision.decided_by,
                "result": cast(dict[str, JsonValue], dict(result)),
            },
        )
        # FILES-CLEANUP-14: the held lanes go on as after keep; failures are in the log
        # and the decision's result.
        self.lanes.resolve_hold(decision.run_id, decision.id)
        self._finish_job(job, "succeeded", result=cast(dict[str, JsonValue], dict(result)))
        settle(
            self.db,
            decision.run_id,
            lambda type, step, d: self._changeset_emitter(decision.run_id, step)(type, d),
        )

    def _apply_prune(self, job: Job) -> None:
        """Drop the albums a person accepted (LIBRARY-PRUNE-3 .. -6), then let the held
        lane go on."""
        payload = job.payload
        decision_id = str(payload.get("decision_id"))
        decision = self.prunes.get(decision_id)
        if decision is None:
            self._finish_job(job, "failed", f"no prune decision {decision_id}")
            return
        emit = self._changeset_emitter(decision.run_id, decision.step_id)
        by, note = payload.get("by"), payload.get("note")
        try:
            dropped = drop_albums(
                self.backend,
                AppRemovalJournal(self.db, self.profile.name),
                decision.run_id,
                [a.album_id for a in decision.albums],
                file_exists(self.backend.library_dirs()),
            )
        except Exception as exc:  # the library couldn't be read: nothing was dropped
            emit("run.step.log", {"stream": "error", "line": f"prune: {type(exc).__name__}: {exc}"})
            result = {"removed": [], "skipped": [], "failed": [a.album_id for a in decision.albums]}
        else:
            for stream, line in dropped.lines:
                emit("run.step.log", {"stream": stream, "line": line})
            result = {
                "removed": dropped.removed,
                "skipped": dropped.skipped,
                "failed": dropped.failed,
            }
        decision.state = "accepted"
        decision.decided_by = by if isinstance(by, str) else None
        decision.decided_at = datetime.now(UTC)
        decision.note = note if isinstance(note, str) else None
        decision.result = result
        self.prunes.release(decision)
        counts: dict[str, JsonValue] = {k: len(v) for k, v in result.items()}
        emit(
            "prune.resolved",
            {
                "decision_id": decision.id,
                "decision": "accept",
                "decided_by": decision.decided_by,
                "result": counts,
            },
        )
        self.lanes.resolve_hold(decision.run_id, decision.id)
        self._finish_job(job, "succeeded", result=counts)
        settle(
            self.db,
            decision.run_id,
            lambda type, step, d: self._changeset_emitter(decision.run_id, step)(type, d),
        )

    def _apply_cover(self, job: Job) -> None:
        """Set the cover a person chose (COVERS-SEARCH-21): a ChangeSet on the album's
        ``art_sha256``, applied through the journal at once (the choice was the review),
        then the album's lane goes on."""
        payload = job.payload
        review_id = str(payload.get("review_id"))
        review = self.covers.get(review_id)
        if review is None:
            self._finish_job(job, "failed", f"no cover review {review_id}")
            return
        sha = str(payload.get("sha256"))
        by = payload.get("by")
        emit = self._changeset_emitter(review.run_id, review.step_id)
        ref = TargetRef(scope="album", id=review.album_id)
        try:
            (album,) = self.backend.query(Selection(ids=(review.album_id,)))
            old = album.fields.get(ART_FIELD)
            rationale = payload.get("rationale")
            cs = ChangeSet(
                id=new_ulid(),
                profile=review.profile,
                run_id=review.run_id,
                step_id=review.step_id,
                source=ChangeSource(kind="step", name="covers.search"),
                rationale=rationale if isinstance(rationale, str) else None,
                created_at=datetime.now(UTC),
                targets=[
                    TargetChange(
                        target=ref,
                        changes=[FieldChange(field=ART_FIELD, old=old, new=sha)],
                    )
                ],
            )
            self.changesets.add(cs)
            emit("changeset.proposed", proposed_event(cs))
            outcome = apply_changeset(
                cs,
                backend=self.backend,
                journal=self.journal,
                store=self.changesets,
                new_id=new_ulid,
                now=lambda: datetime.now(UTC),
                emit=emit,
                resolved_by=by if isinstance(by, str) else None,
            )
        except Exception as exc:  # the album is gone, or the library refused: still waiting
            log.warning("cover review %s: applying failed: %s", review_id, exc)
            self.covers.set_claim(review_id, None)
            message = f"{type(exc).__name__}: {exc}"
            emit("run.step.log", {"stream": "error", "line": f"{review.label}: cover: {message}"})
            self._finish_job(job, "failed", message)
            return
        if outcome.state != "applied":
            self.covers.set_claim(review_id, None)
            self._finish_job(job, "failed", f"the cover change was {outcome.state}")
            return
        review.state = "chosen"
        review.chosen = sha
        review.decided_by = by if isinstance(by, str) else None
        review.decided_at = datetime.now(UTC)
        self.covers.release(review)
        emit(
            "cover.resolved",
            {
                "review_id": review.id,
                "album_id": review.album_id,
                "decision": "choose",
                "decided_by": review.decided_by,
                "sha256": sha,
                "changeset_id": cs.id,
            },
        )
        self.lanes.resolve_hold(review.run_id, review.id)
        self._finish_job(job, "succeeded", result={"changeset_id": cs.id, "sha256": sha})
        settle(
            self.db,
            review.run_id,
            lambda type, step, d: self._changeset_emitter(review.run_id, step)(type, d),
        )

    def _apply_choice(self, job: Job) -> None:
        """Apply what a person chose at a choice review (TAGS-CHOOSE-5): the option's
        actions (or the custom values) run again on the album's current values and become
        one ChangeSet, applied through the journal at once (the choice was the review);
        then the album's lane goes on. ``learn`` and "This is one artist" also update a
        rules file (TAGS-CHOOSE-10, -13)."""
        payload = job.payload
        review_id = str(payload.get("review_id"))
        review = self.choices.get(review_id)
        if review is None:
            self._finish_job(job, "failed", f"no choice review {review_id}")
            return
        by = payload.get("by")
        decided_by = by if isinstance(by, str) else None
        note = payload.get("note")
        note = note if isinstance(note, str) and note.strip() else None
        option = payload.get("option")
        raw_values = payload.get("values")
        values = cast(dict[str, Any], raw_values) if isinstance(raw_values, dict) else None
        known_name = payload.get("known_name") is True
        emit = self._changeset_emitter(review.run_id, review.step_id)
        changeset_id: str | None = None
        learned: list[str] = []
        try:
            label, actions = decision_actions(
                review,
                option=option if isinstance(option, str) else None,
                values=values,
                known_name=known_name,
                escape=self.backend.escape_template,
            )
            if known_name and review.known_name_value is not None:
                learned.append(self.backend.add_artist_name(review.known_name_value))
            (album,) = self.backend.query(Selection(ids=(review.album_id,)))
            if actions:
                script = TagScript.model_validate({"idempotence": "off", "actions": actions})
                result = run_script(script, album, self.backend)
                if result.error is not None:
                    raise ValueError(str(result.error))
                made = tagscript_proposal(result, review.step_id)
                if made is not None:
                    snapshots = SnapshotIndex()
                    snapshots.add([album])
                    rationale = f"{label} (chosen in review)" + (f": {note}" if note else "")
                    built = build_changesets(
                        made.model_copy(update={"rationale": rationale}),
                        current=None,
                        snapshots=snapshots,
                        make=lambda targets, why: new_changeset(
                            targets,
                            why,
                            id=new_ulid(),
                            profile=review.profile,
                            run_id=review.run_id,
                            step_id=review.step_id,
                            source=ChangeSource(kind="step", name="tags.choose"),
                            created_at=datetime.now(UTC),
                        ),
                    )
                    for cs in built:
                        self.changesets.add(cs)
                        emit("changeset.proposed", proposed_event(cs))
                        outcome = apply_changeset(
                            cs,
                            backend=self.backend,
                            journal=self.journal,
                            store=self.changesets,
                            new_id=new_ulid,
                            now=lambda: datetime.now(UTC),
                            emit=emit,
                            resolved_by=decided_by,
                        )
                        if outcome.state != "applied":
                            raise ValueError(f"the change was {outcome.state}")
                        changeset_id = cs.id
            if option == CUSTOM and not known_name and review.learn is not None:
                learned += self._learn(review, values or {})
        except Exception as exc:  # the album is gone, or the library refused: still waiting
            log.warning("choice review %s: applying failed: %s", review_id, exc)
            self.choices.set_claim(review_id, None)
            message = f"{type(exc).__name__}: {exc}"
            emit("run.step.log", {"stream": "error", "line": f"{review.label}: choice: {message}"})
            self._finish_job(job, "failed", message)
            return
        review.state = "chosen"
        review.chosen = "known_name" if known_name else str(option)
        review.decided_by = decided_by
        review.decided_at = datetime.now(UTC)
        review.note = note
        review.changeset_id = changeset_id
        review.learned = "; ".join(learned) or None
        self.choices.release(review)
        for line in learned:
            emit("run.step.log", {"stream": "info", "line": f"{review.label}: {line}"})
        emit(
            "choice.resolved",
            {
                "review_id": review.id,
                "album_id": review.album_id,
                "decision": "choose",
                "option": review.chosen,
                "label": label,
                "decided_by": decided_by,
                "changeset_id": changeset_id,
                "learned": review.learned,
            },
        )
        self.lanes.resolve_hold(review.run_id, review.id)
        result_data: dict[str, JsonValue] = {
            "changeset_id": changeset_id,
            "learned": review.learned,
        }
        self._finish_job(job, "succeeded", result=result_data)
        settle(
            self.db,
            review.run_id,
            lambda type, step, d: self._changeset_emitter(review.run_id, step)(type, d),
        )

    def _learn(self, review: ChoiceReview, values: dict[str, Any]) -> list[str]:
        """TAGS-CHOOSE-10: the album's rank_by values no rule matched get a rule setting
        the custom value of the rules file's target; with none, a value no rule gives yet
        is added as a known value (a rule without a query)."""
        learn = review.learn
        assert learn is not None
        value = values.get(learn.target)
        if value in (None, "", []):
            return []
        path = resolve_rules(self.profile.root, learn.rules_file)
        label = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
        if learn.unmatched:
            # One query term, quoted: backend queries are split at spaces like a shell line.
            when = shlex.quote(
                f"{learn.rank_by}::" + "|".join(query_literal(v) for v in learn.unmatched)
            )
            return [append_rule(path, label, when, learn.target, value)]
        known = set(rule_values(load_rules(path)))
        given = [str(v).strip() for v in (value if isinstance(value, list) else [value])]
        return [
            append_rule(path, v, None, learn.target, [v] if isinstance(value, list) else v)
            for v in dict.fromkeys(given)
            if v and v not in known
        ]

    def _retry_move(self, job: Job) -> None:
        """Plan a conflicted album again with its step's settings (LIBRARY-MOVE-5a). Still
        conflicted: it stays in the queue. Otherwise its FileChangeSet is applied at once
        (the retry was the review) and the album's lane goes on."""
        conflict_id = str(job.payload.get("conflict_id"))
        conflict = self.moves.get(conflict_id)
        if conflict is None:
            self._finish_job(job, "failed", f"no move conflict {conflict_id}")
            return
        by = job.payload.get("by")
        decider = by if isinstance(by, str) else None
        emit = self._changeset_emitter(conflict.run_id, conflict.step_id)
        settings = conflict.settings

        def log_line(stream: str, line: str) -> None:
            emit("run.step.log", {"stream": stream, "line": f"{conflict.label}: {line}"})

        try:
            selection = Selection(ids=(conflict.album_id,), scope="album")
            snapshots = self.backend.query(selection)
            plans = self.backend.plan_moves(
                selection,
                mode=settings.mode,
                formats=settings.formats,
                directory=Path(settings.directory) if settings.directory else None,
                artifacts=settings.artifacts,
            )
            if not snapshots or not plans:
                raise QueryError(f"{conflict.album_id} is no longer in the library")
            plan = plans[0]
        except (QueryError, TemplateError, CapabilityError) as exc:
            plan = None
            problems = [str(exc)]
        else:
            problems = list(plan.conflicts)
        if plan is None or problems:  # still conflicted: keep waiting
            conflict.conflicts = problems
            conflict.attempts += 1
            self.moves.release(conflict)
            for line in problems:
                log_line("error", f"conflict: {line}")
            self._finish_job(job, "failed", "still conflicts: " + "; ".join(problems))
            return
        cs = None
        error: str | None = None
        if plan.files:
            stamp = settings.stamp
            old = snapshots[0].fields.get(stamp.field) if stamp is not None else None
            cs = file_changeset(
                plan,
                stamp,
                old,
                id=new_ulid(),
                profile=conflict.profile,
                run_id=conflict.run_id,
                step_id=conflict.step_id,
                source="library.move",
                now=datetime.now(UTC),
            )
            self.changesets.add(cs)
            emit("changeset.proposed", proposed_event(cs))
            log_line(
                "info", f"{settings.mode} {len(plan.files)} files: {plan.src_dir} -> {plan.dst_dir}"
            )
            outcome = apply_changeset(
                cs,
                backend=self.backend,
                journal=self.journal,
                store=self.changesets,
                new_id=new_ulid,
                now=lambda: datetime.now(UTC),
                emit=emit,
                resolved_by=decider,
            )
            error = outcome.error
        else:
            log_line("info", "already in place")
        conflict.state = "moved" if cs is not None else "in_place"
        conflict.changeset_id = cs.id if cs is not None else None
        conflict.decided_by = decider
        conflict.decided_at = datetime.now(UTC)
        self.moves.release(conflict)
        emit(
            "move.resolved",
            {
                "conflict_id": conflict.id,
                "album_id": conflict.album_id,
                "decision": "retry",
                "decided_by": decider,
                "changeset_id": conflict.changeset_id,
            },
        )
        if error is not None:  # LIBRARY-MOVE-10: a file failed, so the lane fails
            log_line("error", error)
            moved = self.lanes.set_state(
                conflict.run_id, [conflict.album_id], "failed", only_from=("waiting",)
            )
            for key in moved:
                data: dict[str, JsonValue] = {"key": key, "state": "failed", "reason": error}
                emit("lane.finished", data)
        self.lanes.resolve_hold(conflict.run_id, conflict.id)
        result: dict[str, JsonValue] = {"changeset_id": conflict.changeset_id}
        self._finish_job(job, "failed" if error else "succeeded", error, result=result)
        settle(
            self.db,
            conflict.run_id,
            lambda type, step, d: self._changeset_emitter(conflict.run_id, step)(type, d),
        )

    def _lookup_import(self, job: Job) -> None:
        """Look a decision's folder up again and add what it finds (IMPORT-UI-9)."""
        payload = job.payload
        decision_id = str(payload.get("decision_id"))
        decision = self.imports.get(decision_id)
        if decision is None:
            self._finish_job(job, "failed", f"no import decision {decision_id}")
            return
        raw_ids = payload.get("ids")
        ids = [i for i in raw_ids if isinstance(i, str)] if isinstance(raw_ids, list) else []
        artist, album = payload.get("artist"), payload.get("album")
        search = bool(ids or artist or album)
        try:
            found = self.backend.lookup(
                Path(decision.path),
                ids=ids,
                artist=artist if isinstance(artist, str) else None,
                album=album if isinstance(album, str) else None,
            )
        except Exception as exc:
            message = (
                str(exc) if isinstance(exc, ImportApplyError) else f"{type(exc).__name__}: {exc}"
            )
            decision.error = message
            self.imports.release(decision)
            self._finish_job(job, "failed", message)
            return
        decision.fingerprint = found.fingerprint
        decision.items = list(found.items)
        decision.lookup_problem = self.backend.lookup_problem()
        decision.match_thresholds = self.backend.match_thresholds()
        decision.candidates = (
            merge_candidates(decision.candidates, found.candidates)
            if search
            else list(found.candidates)
        )
        decision.error = None
        self.imports.release(decision)
        self._finish_job(
            job,
            "succeeded",
            result={"candidates": len(decision.candidates), "found": len(found.candidates)},
        )

    def _repropose(self, job: Job) -> None:
        """Propose a conflicted ChangeSet again with each ``old`` from the current values;
        the new one replaces it among its run's holds (REVIEW-QUEUE-5, CHANGESET-8)."""
        changeset_id = str(job.payload.get("changeset_id"))
        stored = self.changesets.get(changeset_id)
        if stored is None or stored.state != "conflicted":
            state = stored.state if stored is not None else "missing"
            self._finish_job(job, "failed", f"ChangeSet {changeset_id} is {state}, not conflicted")
            return
        cs = stored.changeset
        accepted = [i for i, ok in sorted(stored.decisions.items()) if ok] or None
        try:
            targets = repropose_targets(cs, current_values(self.backend, cs), accepted)
        except Exception as exc:
            log.warning("re-proposing %s failed: %s", changeset_id, exc)
            self._finish_job(job, "failed", f"{type(exc).__name__}: {exc}")
            return
        new = (
            ChangeSet(
                id=new_ulid(),
                profile=cs.profile,
                run_id=cs.run_id,
                step_id=cs.step_id,
                source=cs.source,
                rationale=cs.rationale,
                created_at=datetime.now(UTC),
                targets=targets,
            )
            if targets
            else None
        )
        by = job.payload.get("by")
        resolved_by = (
            f"re-proposed as {new.id}"
            if new is not None
            else (by if isinstance(by, str) else "re-proposed: nothing left to change")
        )
        if not self.changesets.replace(changeset_id, new, resolved_by=resolved_by):
            self._finish_job(job, "failed", f"ChangeSet {changeset_id} is no longer conflicted")
            return
        emit = self._changeset_emitter(cs.run_id, cs.step_id)
        if new is not None:
            emit("changeset.proposed", {**proposed_event(new), "replaces": changeset_id})
        emit(
            "changeset.rejected",
            {"changeset_id": changeset_id, "replaced_by": new.id if new is not None else None},
        )
        self._finish_job(job, "succeeded", result={"changeset_id": new.id if new else None})
        if cs.run_id is not None:
            self._resume_if_released(cs.run_id)

    # -- undo (JOURNAL-4 .. -7) -----------------------------------------------------

    def _undo(self, job: Job) -> None:
        payload = job.payload
        by = payload.get("by")
        ctx = UndoContext(
            backend=self.backend,
            journal=self.journal,
            store=self.changesets,
            new_id=new_ulid,
            now=lambda: datetime.now(UTC),
            emitter_for=self._changeset_emitter,
            by=by if isinstance(by, str) else None,
            force=frozenset(f for f in forced if isinstance(f, str))
            if isinstance(forced := payload.get("force"), list)
            else frozenset(),
        )
        entry_id, run_id = payload.get("entry_id"), payload.get("run_id")
        try:
            if isinstance(entry_id, str):
                one = undo_entry(entry_id, ctx)
                result: dict[str, JsonValue] = {
                    "undone": [one.entry_id] if one.undone else [],
                    "undo_entries": [one.undo_entry_id] if one.undone else [],
                    "stopped_at": None if one.undone else one.entry_id,
                    "conflicts": [c.as_json() for c in one.conflicts],
                    "problems": [
                        f"{p.target.label()} {p.field}: {p.message}" for p in one.problems
                    ],
                    "changed_files": list(one.changed_files),
                    "skipped_files": list(one.skipped_files),
                }
            elif isinstance(run_id, str):
                result = undo_run(run_id, ctx).as_json()
            else:
                raise UndoError("an undo job needs entry_id or run_id")
        except Exception as exc:
            log.warning("undo failed: %s", exc)
            self._finish_job(job, "failed", f"{type(exc).__name__}: {exc}")
            return
        self._replan_waiting(None)  # LIBRARY-MOVE-19
        stopped = result.get("stopped_at")
        error = f"undo stopped at journal entry {stopped}" if stopped else None
        self._finish_job(job, "failed" if stopped else "succeeded", error, result)

    # -- import undo (F-DASHBOARD DASHBOARD-7 .. -9) --------------------------------

    def _undo_import(self, job: Job) -> None:
        payload = job.payload
        by = payload.get("by")
        run_id = payload.get("run_id")
        albums = payload.get("albums")
        ctx = UndoContext(
            backend=self.backend,
            journal=self.journal,
            store=self.changesets,
            new_id=new_ulid,
            now=lambda: datetime.now(UTC),
            emitter_for=self._changeset_emitter,
            by=by if isinstance(by, str) else None,
        )
        try:
            if not isinstance(run_id, str):
                raise ValueError("an import undo job needs run_id")
            wanted = [str(a) for a in albums] if isinstance(albums, list) else None
            result, error = undo_import(
                self.db,
                self.backend,
                ctx,
                profile=self.profile.name,
                run_id=run_id,
                albums=wanted,
            )
        except Exception as exc:
            log.warning("import undo failed: %s", exc)
            self._finish_job(job, "failed", f"{type(exc).__name__}: {exc}")
            return
        self._finish_job(job, "failed" if error else "succeeded", error, result)

    # -- manual edits (TAG-GRID-8) ------------------------------------------------

    def _apply_manual(self, job: Job) -> None:
        """Apply the tag grid's ChangeSets through the journal, oldest first. Their
        events go on the profile stream (TAG-GRID-9); a conflict or refusal of one
        doesn't stop the others."""
        raw = job.payload.get("changesets")
        ids = [str(i) for i in raw] if isinstance(raw, list) else []  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        by = job.payload.get("by")
        outcomes: dict[str, JsonValue] = {}
        errors: list[str] = []
        touched: set[str] = set()
        for changeset_id in ids:
            stored = self.changesets.get(changeset_id)
            if stored is None or stored.state != "proposed":
                outcomes[changeset_id] = stored.state if stored is not None else "missing"
                continue
            try:
                outcome = apply_changeset(
                    stored.changeset,
                    backend=self.backend,
                    journal=self.journal,
                    store=self.changesets,
                    new_id=new_ulid,
                    now=lambda: datetime.now(UTC),
                    emit=self._changeset_emitter(None, None),
                    resolved_by=by if isinstance(by, str) else None,
                )
            except Exception as exc:  # e.g. the library is locked: the next ones may work
                log.warning("manual ChangeSet %s failed: %s", changeset_id, exc)
                outcomes[changeset_id] = "failed"
                errors.append(f"{changeset_id}: {type(exc).__name__}: {exc}")
                continue
            outcomes[changeset_id] = outcome.state
            if outcome.state == "applied":
                touched |= self._album_ids(stored.changeset)
        if touched:
            self._replan_waiting(touched)
        error = "; ".join(errors) or None
        self._finish_job(job, "failed" if errors else "succeeded", error, {"outcomes": outcomes})

    # -- held edits of an `edit: true` gate (S-REVIEW REVIEW-15) ------------------------

    def _apply_gate_edits(self, job: Job) -> None:
        """Write each album's held edits as one ChangeSet, then continue the albums whose
        edits all applied. An album with a conflicted or refused edit gets nothing written
        and stays undecided with its edits held; the job fails naming them."""
        payload = job.payload
        gate = self.gates.get(str(payload.get("gate_id")))
        if gate is None or gate.state != "pending":
            self._finish_job(job, "failed", "the review gate is no longer pending")
            return
        raw = payload.get("albums")
        wanted = [str(a) for a in raw] if isinstance(raw, list) else gate.undecided()  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        albums = [a for a in wanted if a in gate.undecided()]
        by = payload.get("by")
        decider = by if isinstance(by, str) else None
        note = payload.get("note")
        emit = self._changeset_emitter(gate.run_id, gate.step_id)
        done: list[str] = []
        errors: list[str] = []
        for album in albums:
            held = [e for e in gate.edits if e.album == album]
            if not held:
                done.append(album)
                continue
            cs = ChangeSet(
                id=new_ulid(),
                profile=gate.profile,
                run_id=gate.run_id,
                step_id=gate.step_id,
                source=ChangeSource(kind="manual", name="review"),
                rationale="edited at the review",
                created_at=datetime.now(UTC),
                targets=[TargetChange(target=e.target, changes=list(e.changes)) for e in held],
            )
            self.changesets.add(cs)
            emit("changeset.proposed", proposed_event(cs))
            try:
                outcome = apply_changeset(
                    cs,
                    backend=self.backend,
                    journal=self.journal,
                    store=self.changesets,
                    new_id=new_ulid,
                    now=lambda: datetime.now(UTC),
                    emit=emit,
                    resolved_by=decider,
                )
            except Exception as exc:
                outcome = None
                problem = f"{type(exc).__name__}: {exc}"
            else:
                problem = _problem(outcome)
            if outcome is not None and outcome.state == "applied":
                done.append(album)
                continue
            stored = self.changesets.get(cs.id)
            if stored is not None and stored.state in ("proposed", "conflicted"):
                # It mustn't hold the run (CHANGESET-7): the album's edits stay held.
                reject_changeset(cs.id, store=self.changesets, emit=emit, resolved_by=decider)
            label = next((a.label for a in gate.albums if a.target.id == album), album)
            errors.append(f"{label}: {problem}")
            emit("run.step.log", {"stream": "error", "line": f"{label}: not written: {problem}"})
        if done:
            self._continue_gate_albums(gate, done, decider, note if isinstance(note, str) else None)
        error = "; ".join(errors) or None
        result: dict[str, JsonValue] = {"continued": list[JsonValue](done)}
        self._finish_job(job, "failed" if errors else "succeeded", error, result)

    def _continue_gate_albums(
        self, gate: ReviewGate, albums: list[str], by: str | None, note: str | None
    ) -> None:
        """Decide ``albums`` of ``gate`` as continued, as the supervisor does without held
        edits (REVIEW-7, DASHBOARD-11), and let the run go on once nothing holds it."""
        current = self.gates.get(gate.id) or gate
        self.gates.set_edits(gate.id, [e for e in current.edits if e.album not in albums])
        final = set(albums) == set(current.undecided())
        data: dict[str, JsonValue] = {
            "gate_id": gate.id,
            "decision": "continue",
            "decided_by": by,
            "albums": list[JsonValue](albums),
        }
        if note is not None:
            data["note"] = note
        emit = self._changeset_emitter(gate.run_id, gate.step_id)
        if not final or current.album_decisions:
            self.gates.decide_albums(gate.id, albums, "continued")
        if final:
            decided = self.gates.decide(gate.id, "continued", by=by, note=note)
            data["decided_at"] = decided.decided_at.isoformat() if decided.decided_at else None
            emit("review.resolved", data)
        else:
            emit("review.decided", data)
            self.lanes.drop_hold(gate.run_id, albums, gate.id)
        self._resume_if_released(gate.run_id)

    # -- a waiting move follows its album (S-LIBRARY-MOVE LIBRARY-MOVE-19) ------------

    def _album_ids(self, cs: ChangeSet) -> set[str]:
        """The albums a ChangeSet touches: its album targets and its items' albums."""
        albums = {t.target.id for t in cs.targets if t.target.scope == "album"}
        items = tuple(t.target.id for t in cs.targets if t.target.scope == "item")
        if items:
            try:
                found = self.backend.query(Selection(ids=items, scope="item"))
            except QueryError:
                found = []
            albums |= {a.id for a in found}
        return albums

    def _replan_waiting(self, albums: set[str] | None) -> None:
        """Plan the waiting moves of ``albums`` (all, for ``None``) again."""
        for stored in self.changesets.waiting_moves(self.profile.name):
            cs = stored.changeset
            if cs.album is not None and (albums is None or cs.album.id in albums):
                try:
                    self._refresh_move(cs)
                except Exception as exc:  # one album's trouble mustn't stop the job
                    log.warning("re-planning %s failed: %s", cs.id, exc)

    def _refresh_move(self, cs: ChangeSet) -> tuple[ChangeSet, list[str]]:
        """Plan ``cs``'s move again and store a changed plan under the same id. Returns the
        ChangeSet to apply and the conflicts that stop it (then nothing changes)."""
        new, problems = self._replan(cs)
        emit = self._changeset_emitter(cs.run_id, cs.step_id)
        label = cs.album.label() if cs.album is not None else cs.id
        for line in problems:
            emit(
                "run.step.log",
                {"stream": "error", "line": f"{label}: the move conflicts now: {line}"},
            )
        if new is not cs and self.changesets.update_proposed(new):
            emit(
                "changeset.replanned",
                {"changeset_id": new.id, "album_id": label, "files": len(new.files)},
            )
            emit("run.step.log", {"stream": "info", "line": f"{label}: move planned again"})
            return new, problems
        return cs, problems

    def _replan(self, cs: ChangeSet) -> tuple[ChangeSet, list[str]]:
        if cs.replan is None or cs.album is None or cs.run_id is None or cs.step_id is None:
            return cs, []
        settings = MoveSettings.model_validate(cs.replan)
        selection = Selection(ids=(cs.album.id,), scope="album")
        try:
            snapshots = self.backend.query(selection)
            plans = self.backend.plan_moves(
                selection,
                mode=settings.mode,
                formats=settings.formats,
                directory=Path(settings.directory) if settings.directory else None,
                artifacts=settings.artifacts,
            )
            if not snapshots or not plans:
                raise QueryError(f"{cs.album.id} is no longer in the library")
        except (QueryError, TemplateError, CapabilityError) as exc:
            return cs, [str(exc)]
        plan = plans[0]
        if plan.conflicts:
            return cs, list(plan.conflicts)
        if not plan.files:
            return cs, ["its files are already where the move would put them"]
        stamp = settings.stamp
        new = file_changeset(
            plan,
            stamp,
            snapshots[0].fields.get(stamp.field) if stamp is not None else None,
            id=cs.id,
            profile=cs.profile,
            run_id=cs.run_id,
            step_id=cs.step_id,
            source=cs.source.name,
            now=datetime.now(UTC),
            show=cs.show,
        ).model_copy(update={"replan": cs.replan, "created_at": cs.created_at})
        if new.files == cs.files and _olds(new) == _olds(cs):
            return cs, []
        return new, []

    def _fail_move_lane(self, cs: ChangeSet, error: str) -> None:
        """LIBRARY-MOVE-10: a move that failed part way fails its album's lane."""
        if cs.run_id is None or cs.album is None:
            return
        emit = self._changeset_emitter(cs.run_id, cs.step_id)
        emit("run.step.log", {"stream": "error", "line": f"{cs.album.label()}: {error}"})
        for key in self.lanes.set_state(cs.run_id, [cs.album.id], "failed", only_from=("waiting",)):
            emit("lane.finished", {"key": key, "state": "failed", "reason": error})

    def _finish_job(
        self, job: Job, state: JobState, error: str | None = None, result: JsonValue = None
    ) -> None:
        """Finish a job that isn't a run and say so on its run's stream, or on the profile
        stream for one without a run (TAG-GRID-9, -11, REVIEW-QUEUE-10)."""
        self.db.finish_job(job.id, state, error, result)
        data: dict[str, JsonValue] = {
            "job_id": job.id,
            "kind": job.kind,
            "state": state,
            "error": error,
            "result": result,
        }
        self._changeset_emitter(job.run_id, None)("job.finished", data)

    def _resume_if_released(self, run_id: str) -> None:
        self.db.requeue_if_released(run_id)  # the serve loop claims it next (RUN-14)

    # -- rejected at a review gate (REVIEW-8) -----------------------------------------

    def _reject_at_review(self, job: Job) -> None:
        """``on_reject: undo``: undo the gate's albums' applied ChangeSets of this run
        newest first, log what happened, then end those albums' lanes ``rejected`` and
        settle the run (LANES-11). A run without lanes is undone and ended as a whole."""
        run_id = job.run
        gate_id = job.payload.get("gate_id")
        gate = self.gates.get(str(gate_id)) if isinstance(gate_id, str) else None
        step_id = gate.step_id if gate is not None else None

        def log_line(line: str, stream: str = "info") -> None:
            self._changeset_emitter(run_id, step_id)(
                "run.step.log", {"stream": stream, "line": line}
            )

        run_job = self.db.job_for_run(run_id)
        if run_job is None or run_job.state != "waiting_review":
            state = run_job.state if run_job is not None else "missing"
            self.db.finish_job(job.id, "failed", f"run {run_id} is {state}, not waiting")
            return
        by = job.payload.get("by")
        ctx = UndoContext(
            backend=self.backend,
            journal=self.journal,
            store=self.changesets,
            new_id=new_ulid,
            now=lambda: datetime.now(UTC),
            emitter_for=self._changeset_emitter,
            by=by if isinstance(by, str) else None,
        )
        result: dict[str, JsonValue] = {}
        chosen = job.payload.get("albums")
        albums = [str(a) for a in chosen] if isinstance(chosen, list) else None
        targets = self._gate_targets(run_id, gate, albums)
        try:
            outcome = undo_run(run_id, ctx, targets=targets)
            result = outcome.as_json()
            if not outcome.undone and outcome.stopped is None:
                log_line("rejected: the run has no applied tag changes to undo")
            for done in outcome.undone:
                log_line(f"rejected: undid journal entry {done.entry_id}")
            if outcome.stopped is not None:
                stopped = outcome.stopped
                details = [
                    f"{c.target.label()} {c.field}: expected {c.expected!r}, now {c.current!r}"
                    for c in stopped.conflicts
                ] + [f"{p.target.label()} {p.field}: {p.message}" for p in stopped.problems]
                log_line(
                    f"undo stopped at journal entry {stopped.entry_id}; older changes stay: "
                    + "; ".join(details),
                    "error",
                )
            imported = self._imported_count(run_id, targets)
            if imported:
                log_line(
                    f"{imported} album(s) this run imported stay in the library: "
                    "undo doesn't remove imports"
                )
        except Exception as exc:  # the run still ends rejected
            log.warning("undo of rejected run %s failed: %s", run_id, exc)
            log_line(f"undo failed: {type(exc).__name__}: {exc}", "error")
            result = {"error": f"{type(exc).__name__}: {exc}"}

        def emit(type: str, step: str | None, data: dict[str, JsonValue]) -> None:
            self._changeset_emitter(run_id, step)(type, data)

        if gate is not None:
            reject_gate_lanes(self.db, gate, emit, albums=albums)
        stopped_at = result.get("stopped_at") if isinstance(result, dict) else None
        error = f"undo stopped at journal entry {stopped_at}" if stopped_at else None
        self.db.finish_job(job.id, "failed" if error else "succeeded", error, result)

    def _gate_targets(
        self, run_id: str, gate: ReviewGate | None, chosen: list[str] | None = None
    ) -> set[str] | None:
        """The gate's albums (or ``chosen`` of them, DASHBOARD-11) and their items, when
        the run has lanes (LANES-11)."""
        if gate is None or not self.lanes.load(run_id):
            return None
        albums = gate_albums(gate) if chosen is None else chosen
        targets = set(albums)
        try:
            for album in self.backend.query(Selection(run_id=run_id, album_ids=tuple(albums))):
                targets.update(item.id for item in album.items)
        except CapabilityError:
            pass
        return targets

    def _imported_count(self, run_id: str, targets: set[str] | None = None) -> int:
        try:
            albums = self.backend.query(Selection.for_run(run_id))
        except CapabilityError:
            return 0
        return len([a for a in albums if targets is None or a.id in targets])

    def _changeset_emitter(
        self, run_id: str | None, step_id: str | None
    ) -> Callable[[str, dict[str, JsonValue]], None]:
        # ChangeSets without a run go on the profile's stream (TAG-GRID-9).
        stream = run_id if run_id is not None else profile_stream(self.profile.name)

        def emit(type: str, data: dict[str, JsonValue]) -> None:
            seq = self._next_seq(stream)
            self._sent[stream] = seq
            event = Event(
                seq=seq, ts=datetime.now(UTC), run_id=stream, type=type, step_id=step_id, data=data
            )
            self.send(WorkerMessage(kind="event", event=event))

        return emit

    def _send_finished(self, run_id: str, seq: int, error: str) -> None:
        event = Event(
            seq=seq,
            ts=datetime.now(UTC),
            run_id=run_id,
            type="run.finished",
            data={"status": "failed", "error": error},
        )
        self.send(WorkerMessage(kind="event", event=event))


def step_records(events: Iterable[Event]) -> dict[str, StepRecord]:
    """Records of finished steps, rebuilt from a run's stored events (RUN-14)."""
    uses: dict[str, str] = {}
    started: dict[str, datetime] = {}
    records: dict[str, StepRecord] = {}
    for e in events:
        if e.step_id is None:
            continue
        if e.type == "run.step.started":
            uses[e.step_id] = str(e.data.get("uses", ""))
            started[e.step_id] = e.ts
        elif e.type == "review.resolved" and e.step_id in records:
            # The decision shows on the gate step's record (REVIEW-7).
            record = records[e.step_id]
            for key in ("decision", "decided_by", "decided_at", "note"):
                value = e.data.get(key)
                if isinstance(value, str):
                    record.outputs[key] = value
                    if record.passes:  # the latest pass is the gate's (LANES-4)
                        record.passes[-1].outputs[key] = value
        elif e.type == "run.step.finished":
            data = e.data
            outputs = data.get("outputs") or {}
            failed = data.get("failed_targets") or []
            assert isinstance(outputs, dict)
            assert isinstance(failed, list)
            error = data.get("error")
            previous = records.get(e.step_id)
            record = StepRecord(
                id=e.step_id,
                uses=uses.get(e.step_id, ""),
                status=data.get("status"),  # type: ignore[arg-type]
                outputs={
                    str(k): v for k, v in outputs.items() if isinstance(v, str | int | float | bool)
                },
                failed_targets=[
                    FailedTarget(str(f.get("target")), str(f.get("message")))
                    for f in failed
                    if isinstance(f, dict)
                ],
                error=error if isinstance(error, str) else None,
                started_at=started.get(e.step_id),
                finished_at=e.ts,
                passes=list(previous.passes) if previous is not None else [],
            )
            number = data.get("pass")
            lanes = data.get("lanes")
            record.passes.append(  # LANES-4: one entry per pass
                PassRecord(
                    number=number if isinstance(number, int) else len(record.passes) + 1,
                    lanes=lanes if isinstance(lanes, int) else 0,
                    status=record.status,
                    outputs=dict(record.outputs),
                    error=record.error,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                )
            )
            records[e.step_id] = record
    return records


# -- process ------------------------------------------------------------------


def _lines(fd: int, chunk: int = 65536) -> Iterator[bytes]:
    """Lines from a raw file descriptor until EOF.

    Plain ``os.read`` instead of ``sys.stdin.buffer``: a daemon thread blocked inside a
    ``BufferedReader`` holds its lock, and interpreter shutdown then aborts with
    "could not acquire lock for <_io.BufferedReader name='<stdin>'>" (B-022).
    """
    pending = b""
    while data := os.read(fd, chunk):
        pending += data
        *lines, pending = pending.split(b"\n")
        yield from lines
    if pending:
        yield pending


def _read_commands(
    lines: Iterable[bytes], loop: asyncio.AbstractEventLoop, queue: Commands
) -> None:
    def put(item: WorkerCommand | None) -> None:
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(queue.put_nowait, item)

    for line in lines:
        if not line.strip():
            continue
        try:
            put(WorkerCommand.model_validate_json(line))
        except ValidationError as exc:
            log.warning("ignoring malformed command: %s", exc.errors()[0]["msg"])
    put(None)


def _heartbeat(send: Send, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        send(WorkerMessage(kind="heartbeat"))


def _claim_channel() -> Channel:
    """Move the real stdout to a private fd; point fd 1 (and sys.stdout) at stderr."""
    sys.stdout.flush()
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    return Channel(out)


def main(
    argv: list[str] | None = None,
    *,
    backend_factory: Callable[[Profile], LibraryBackend] | None = None,
    registry: StepTypeRegistry | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
) -> int:
    parser = argparse.ArgumentParser(prog="python -m rifferr.worker")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="preview and query requests only; the library is opened read-only (WORKER-11)",
    )
    args = parser.parse_args(argv)
    channel = _claim_channel()
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format=f"[worker {args.profile}] %(message)s"
    )
    stop = threading.Event()
    threading.Thread(
        target=_heartbeat, args=(channel.send, heartbeat_interval, stop), daemon=True
    ).start()
    if args.read_only:
        try:
            return _main_read_only(args.profile, channel, backend_factory)
        finally:
            stop.set()
    try:
        profile = load_profile(config_root(), args.profile)
        backend = (backend_factory or load_backend)(profile)
        problems = [*backend.plugin_problems(), backend.lookup_problem()]  # BEETS-IMPORT-19, -20
        for problem in problems:
            if problem is not None:
                log.warning("[worker %s] %s", profile.name, problem)
        steps = registry or load_step_types()
        for problem in steps.errors:
            log.warning("%s", problem)
        db = AppDB(args.db or app_db_path())
    except Exception as exc:
        channel.send(WorkerMessage(kind="error", message=f"worker failed to start: {exc}"))
        return 2
    secrets = ProfileSecrets(profile.secrets, SecretResolver())

    async def serve() -> None:
        queue: Commands = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(
            target=_read_commands, args=(_lines(sys.stdin.fileno()), loop, queue), daemon=True
        ).start()
        worker = Worker(
            profile,
            db,
            steps,
            backend,
            channel.send,
            secrets=secrets,
            idle_timeout=profile.worker.idle_timeout,
            notify=ProfileTargets(profile.notify, secrets, default_notifier()),
        )
        await worker.serve(queue)

    try:
        asyncio.run(serve())
    finally:
        stop.set()
        db.close()
    return 0


def _main_read_only(
    profile_name: str,
    channel: Channel,
    backend_factory: Callable[[Profile], LibraryBackend] | None,
) -> int:
    from rifferr.worker.readonly import ReadOnlyWorker

    try:
        profile = load_profile(config_root(), profile_name)
        if backend_factory is not None:
            backend = backend_factory(profile)
        else:
            backend = load_backend(profile, read_only=True)
        worker = ReadOnlyWorker(
            profile, backend, channel.send, idle_timeout=profile.worker.idle_timeout
        )
    except Exception as exc:
        channel.send(WorkerMessage(kind="error", message=f"worker failed to start: {exc}"))
        return 2

    async def serve() -> None:
        queue: Commands = asyncio.Queue()
        loop = asyncio.get_running_loop()
        threading.Thread(
            target=_read_commands, args=(_lines(sys.stdin.fileno()), loop, queue), daemon=True
        ).start()
        await worker.serve(queue)

    asyncio.run(serve())
    return 0


def _olds(cs: ChangeSet) -> list[tuple[str, str, str]]:
    """What a ChangeSet expects to find: each field change's target, field and old value."""
    return [(t.target.id, c.field, repr(c.old)) for t in cs.targets for c in t.changes]


def _problem(outcome: ApplyOutcome) -> str:
    """Why a held edit wasn't written: its conflicts or the backend's refusals."""
    if outcome.problems:
        return "; ".join(f"{p.target.label()} {p.field}: {p.message}" for p in outcome.problems)
    if outcome.entry is not None and outcome.entry.conflicts:
        return "; ".join(
            f"{c.target.label()} {c.field}: changed meanwhile to {c.current!r}"
            for c in outcome.entry.conflicts
        )
    return outcome.error or outcome.state
