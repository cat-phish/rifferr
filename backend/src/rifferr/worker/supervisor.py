"""Server side of F-WORKER: start, watch and stop one worker process per profile.

The server never imports beets (WORKER-1): everything library-related happens
in the worker. Requests go through the job table first (WORKER-3); the pipe
only wakes the worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import JsonValue, ValidationError

from rifferr.appdb import (
    AppChangeSets,
    AppCleanupDecisions,
    AppCoverReviews,
    AppDB,
    AppImportDecisions,
    AppJournal,
    AppLanes,
    AppReviewGates,
    GateAlbumsError,
    GateResolvedError,
    Job,
)
from rifferr.core.changeset import ChangeSet
from rifferr.core.covers import CoverChoice
from rifferr.core.files import CleanupChoice
from rifferr.core.imports import ImportAction
from rifferr.core.review import GateDecision
from rifferr.engine.events import Event, EventSink, profile_stream
from rifferr.engine.undo import run_entries, undoable
from rifferr.trust import workflow_hash
from rifferr.worker.gates import cancel_lanes, reject_gate_lanes, settle
from rifferr.worker.protocol import (
    HUNG_AFTER,
    MAX_MESSAGE,
    WorkerCommand,
    WorkerMessage,
    encode,
)

log = logging.getLogger("rifferr.worker")

STOP_GRACE = 10.0
"""Seconds a worker gets to exit after a stop request (WORKER-8)."""
WATCHDOG_INTERVAL = 1.0
GATE_CHECK_INTERVAL = 60.0
"""Longest wait between checks for overdue review gates (REVIEW-9)."""

Command = Callable[[str], list[str]]
Listener = Callable[[Event], None]
ProblemListener = Callable[[str, str, str | None], None]
"""profile, reason (``worker_crashed`` / ``worker_hung`` / ``worker_failed_to_start``),
error text."""
ExitReason = Literal["worker_crashed", "worker_hung", "worker_failed_to_start", "server_shutdown"]


@dataclass
class _Handle:
    profile: str
    proc: asyncio.subprocess.Process
    last_seen: float
    ready: bool = False
    stopping: bool = False
    kill_reason: ExitReason | None = None
    start_error: str | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    exited: asyncio.Event = field(default_factory=asyncio.Event)


class WorkerSupervisor:
    def __init__(
        self,
        db: AppDB,
        sink: EventSink,
        *,
        command: Command | None = None,
        hung_after: float = HUNG_AFTER,
        stop_grace: float = STOP_GRACE,
        watchdog_interval: float = WATCHDOG_INTERVAL,
        gate_check_interval: float = GATE_CHECK_INTERVAL,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db = db
        self.sink = sink
        self._command = command or self.default_command
        self.hung_after = hung_after
        self.stop_grace = stop_grace
        self.watchdog_interval = watchdog_interval
        self._workers: dict[str, _Handle] = {}
        self._listeners: list[Listener] = []
        self._problem_listeners: list[ProblemListener] = []
        self._shutting_down = False
        self._lock = asyncio.Lock()
        self.gate_check_interval = gate_check_interval
        self._clock = clock or (lambda: datetime.now(UTC))
        self._gate_timer: asyncio.Task[None] | None = None
        self._gates_changed = asyncio.Event()

    def default_command(self, profile: str) -> list[str]:
        """The server's own interpreter, so the worker sees the same installed plugins."""
        return [
            sys.executable,
            "-m",
            "rifferr.worker",
            "--profile",
            profile,
            "--db",
            str(self.db.path),
        ]

    # -- public API ----------------------------------------------------------

    def subscribe(self, listener: Listener) -> None:
        """Called with each worker event after it has been persisted (WORKER-4)."""
        self._listeners.append(listener)

    def subscribe_problems(self, listener: ProblemListener) -> None:
        """Called when a worker crashes, hangs or can't start; not for idle exits or a
        server shutdown (S-NOTIFY ``worker.problem``)."""
        self._problem_listeners.append(listener)

    async def submit_run(
        self,
        profile: str,
        source: str,
        *,
        source_name: str,
        run_vars: dict[str, str] | None = None,
        workflow: str | None = None,
        trigger: str = "manual",
        run_input: str | None = None,
        called: Mapping[str, str] | None = None,
    ) -> str:
        """Queue a run and make sure the profile's worker is up. Returns the run id.

        ``called``: text of every workflow file the workflow calls, by path relative to
        the profile's workflows/ folder (S-SUBWORKFLOW). They are stored with the job,
        and the run is trusted only when the caller and every one of them is
        (SUBWORKFLOW-12, -13).

        ``trigger`` / ``run_input`` become ``{{ run.trigger }}`` / ``{{ run.input }}``
        (F-TRIGGERS TRIGGERS-5); without an input the workflow's ``input`` applies.

        The workflow *text* is stored with the job, so the worker runs exactly what
        the server checked. Trust is decided here, from the hash of that text
        (SECURITY-10).
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        digest = workflow_hash(source)
        called_hashes = {path: workflow_hash(text) for path, text in (called or {}).items()}
        stored: list[JsonValue] = [
            {"path": path, "source": text, "sha256": called_hashes[path]}
            for path, text in sorted((called or {}).items())
        ]
        hashes = [digest, *called_hashes.values()]
        payload: dict[str, JsonValue] = {
            "source": source,
            "source_name": source_name,
            "vars": dict(run_vars or {}),
            "trusted": all(self.db.is_trusted(h, profile) for h in hashes),
            "trigger": trigger,
            "input": run_input or "",
        }
        if stored:
            payload["called"] = stored
        job = self.db.submit(
            profile,
            payload,
            workflow=workflow,
            workflow_hash=digest,
            trigger=trigger,
            input=run_input or None,
        )
        await self._wake(profile)
        return job.run

    async def decide(
        self,
        changeset_id: str,
        *,
        accept: bool,
        accepted: list[int] | None = None,
        by: str | None = None,
        note: str | None = None,
    ) -> str:
        """Queue a review decision on a ChangeSet as a worker job (CHANGESET-13).

        ``accept=False`` rejects it; ``accepted`` accepts only those field-change
        indexes (CHANGESET-9). Returns the job id. Raises ``KeyError`` for an unknown
        ChangeSet and ``ValueError`` for one that no longer waits for a decision.
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        stored = AppChangeSets(self.db).get(changeset_id)
        if stored is None:
            raise KeyError(changeset_id)
        if stored.state not in ("proposed", "conflicted"):
            raise ValueError(f"ChangeSet {changeset_id} is already {stored.state}")
        if accept and stored.state == "conflicted":
            raise ValueError(f"ChangeSet {changeset_id} conflicted; reject it or propose it again")
        if accepted is not None:
            total = len(stored.changeset.field_changes())
            if not accepted or not set(accepted) <= set(range(total)):
                raise ValueError(f"accepted field changes must be indexes 0..{total - 1}")
        payload: dict[str, JsonValue] = {
            "changeset_id": changeset_id,
            "decision": "accept" if accept else "reject",
            "accepted": cast(list[JsonValue], sorted(set(accepted))) if accepted else None,
            "by": by,
        }
        if note is not None:
            payload["note"] = note
        cs = stored.changeset
        job = self.db.submit_job(cs.profile, "review_decision", payload, cs.run_id)
        await self._wake(cs.profile)
        return job.id

    async def repropose(self, changeset_id: str, *, by: str | None = None) -> str:
        """Queue a job that proposes a conflicted ChangeSet again on the current values
        (REVIEW-QUEUE-5). Returns the job id. Raises ``KeyError`` for an unknown
        ChangeSet and ``ValueError`` for one that isn't conflicted."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        stored = AppChangeSets(self.db).get(changeset_id)
        if stored is None:
            raise KeyError(changeset_id)
        if stored.state != "conflicted":
            raise ValueError(f"ChangeSet {changeset_id} is {stored.state}, not conflicted")
        cs = stored.changeset
        payload: dict[str, JsonValue] = {"changeset_id": changeset_id, "by": by}
        job = self.db.submit_job(cs.profile, "repropose", payload, cs.run_id)
        await self._wake(cs.profile)
        return job.id

    async def undo(
        self, *, entry_id: str | None = None, run_id: str | None = None, by: str | None = None
    ) -> str:
        """Queue an undo of one journal entry or of a whole run as a worker job (C-JOURNAL).

        Returns the job id; its ``result`` lists what was undone and the conflict that
        stopped it, if any. Raises ``KeyError`` for an unknown entry or run and
        ``ValueError`` (``UndoError``) when there is nothing to undo.
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        if (entry_id is None) == (run_id is None):
            raise ValueError("undo needs exactly one of entry_id or run_id")
        journal = AppJournal(self.db)
        payload: dict[str, JsonValue] = {"entry_id": entry_id, "run_id": run_id, "by": by}
        if entry_id is not None:
            found = journal.get(entry_id)
            if found is None:
                raise KeyError(entry_id)
            entry = undoable(found, entry_id)
            profile, ref_run = entry.profile, entry.run_id
        else:
            assert run_id is not None
            job = self.db.job_for_run(run_id)
            if job is None:
                raise KeyError(run_id)
            if job.state in ("queued", "running"):
                raise ValueError(f"run {run_id} is {job.state}; undo it once it has stopped")
            if not run_entries(journal, run_id):
                raise ValueError(f"run {run_id} has no applied changes to undo")
            profile, ref_run = job.profile, run_id
        job = self.db.submit_job(profile, "undo", payload, ref_run)
        await self._wake(profile)
        return job.id

    async def import_undo(
        self, run_id: str, *, albums: list[str] | None = None, by: str | None = None
    ) -> str:
        """Queue the undo of a run's import (F-DASHBOARD DASHBOARD-7) as a worker job.

        Raises ``KeyError`` for an unknown run and ``ValueError`` while it is queued or
        running, or when ``albums`` is given but empty.
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        job = self.db.job_for_run(run_id)
        if job is None:
            raise KeyError(run_id)
        if job.state in ("queued", "running"):
            raise ValueError(f"run {run_id} is {job.state}; undo its import once it has stopped")
        if albums is not None and not albums:
            raise ValueError("no albums to remove")
        payload: dict[str, JsonValue] = {
            "run_id": run_id,
            "albums": list[JsonValue](albums) if albums is not None else None,
            "by": by,
        }
        undo = self.db.submit_job(job.profile, "import_undo", payload, run_id)
        await self._wake(job.profile)
        return undo.id

    async def apply_manual(
        self, profile: str, changesets: list[ChangeSet], *, by: str | None = None
    ) -> tuple[str, list[str]]:
        """Store the tag grid's ChangeSets and queue one job that applies them
        (TAG-GRID-8). Returns the job id and the runs it waits for (TAG-GRID-13)."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        if not changesets:
            raise ValueError("no changes to apply")
        store = AppChangeSets(self.db)
        for cs in changesets:
            store.add(cs)
        ahead = [
            j.run
            for j in self.db.jobs(profile)
            if j.kind == "run" and j.state in ("running", "queued")
        ]
        payload: dict[str, JsonValue] = {
            "changesets": [cs.id for cs in changesets],
            "by": by,
        }
        job = self.db.submit_job(profile, "manual_apply", payload, None)
        await self._wake(profile)
        return job.id, ahead

    # -- review gates (S-REVIEW) ---------------------------------------------------

    async def decide_gate(
        self,
        gate_id: str,
        decision: GateDecision,
        *,
        by: str | None = None,
        note: str | None = None,
        albums: list[str] | None = None,
    ) -> None:
        """Decide a pending review gate, exactly once per album (REVIEW-6 .. -8).

        ``continue`` needs no worker job: the run is queued to resume once nothing else
        holds it (REVIEW-12). ``reject`` ends the run here (``on_reject: stop``) or
        queues a worker job that undoes the run's changes first (``undo``). With
        ``albums`` only those albums' lanes are decided; the gate stays pending until
        every album is (DASHBOARD-11). Raises ``KeyError`` for an unknown gate,
        ``GateResolvedError`` when it was already decided or cancelled, ``GateAlbumsError``
        for albums that aren't undecided albums of the gate, and ``ValueError`` while
        its run isn't waiting yet.
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        gates = AppReviewGates(self.db)
        gate = gates.get(gate_id)
        if gate is None:
            raise KeyError(gate_id)
        if gate.state != "pending":
            raise GateResolvedError(gate_id, gate.state)
        job = self.db.job_for_run(gate.run_id)
        if job is None or job.state != "waiting_review":
            state = job.state if job is not None else "missing"
            raise ValueError(f"run {gate.run_id} is {state}; the gate can't be decided yet")
        undecided = gate.undecided()
        targets = list(dict.fromkeys(albums)) if albums is not None else undecided
        if albums is not None:
            wrong = [a for a in targets if a not in undecided]
            if wrong:
                raise GateAlbumsError(f"not undecided albums of gate {gate_id}: {', '.join(wrong)}")
            if set(targets) != set(undecided) and not AppLanes(self.db).load(gate.run_id):
                raise GateAlbumsError("this run has no lanes; decide the whole gate")
        final = set(targets) == set(undecided)
        verdict: Literal["continued", "rejected"] = (
            "continued" if decision == "continue" else "rejected"
        )
        if albums is not None or gate.album_decisions:
            gates.decide_albums(gate_id, targets, verdict)
        data: dict[str, JsonValue] = {
            "gate_id": gate_id,
            "decision": decision,
            "decided_by": by,
            "albums": list[JsonValue](targets),
        }
        if note is not None:
            data["note"] = note
        if final:
            decided = gates.decide(gate_id, verdict, by=by, note=note)
            data["decided_at"] = decided.decided_at.isoformat() if decided.decided_at else None
            self._emit_run(gate.run_id, "review.resolved", gate.step_id, data)
        else:
            decided = gates.get(gate_id) or gate
            self._emit_run(gate.run_id, "review.decided", gate.step_id, data)
        if decision == "continue":
            if not final:
                AppLanes(self.db).drop_hold(gate.run_id, targets, gate_id)
            if self.db.requeue_if_released(gate.run_id):
                await self._wake(gate.profile)
        elif gate.on_reject == "undo":
            payload: dict[str, JsonValue] = {
                "gate_id": gate_id,
                "run_id": gate.run_id,
                "by": by,
                "albums": list[JsonValue](targets),
            }
            self.db.submit_job(gate.profile, "review_reject", payload, gate.run_id)
            await self._wake(gate.profile)
        else:
            run_id = gate.run_id
            outcome = reject_gate_lanes(
                self.db,
                decided,
                lambda t, step, d: self._emit_run(run_id, t, step, d),
                albums=targets,
            )
            if outcome.settled == "requeued":  # another lane was released meanwhile
                await self._wake(gate.profile)

    async def decide_overdue_gates(self) -> list[str]:
        """Decide every pending gate whose deadline passed with its ``on_timeout``
        (REVIEW-9). Returns the ids decided. Also runs at server start-up."""
        decided: list[str] = []
        for gate in AppReviewGates(self.db).overdue(self._clock()):
            try:
                await self.decide_gate(gate.id, gate.on_timeout or "reject", by="timeout")
            except (GateResolvedError, ValueError, KeyError) as exc:
                log.info("review gate %s: timeout decision skipped: %s", gate.id, exc)
                continue
            decided.append(gate.id)
        return decided

    # -- import decisions (F-IMPORT-UI) ----------------------------------------------

    async def decide_import(
        self,
        decision_id: str,
        action: ImportAction,
        *,
        candidate_id: str | None = None,
        by: str | None = None,
        note: str | None = None,
    ) -> str:
        """Queue the job that imports a decided folder (IMPORT-UI-8, -11). Returns the
        job id. Raises ``KeyError`` (unknown decision), ``ImportResolvedError`` (decided
        already), ``ImportBusyError`` (a job holds it) or ``ValueError`` (bad action)."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        store = AppImportDecisions(self.db)
        decision = store.get(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        if not decision.allows(action):
            raise ValueError(f"action {action!r} doesn't answer a {decision.kind} decision")
        if action == "candidate":
            if candidate_id is None:
                raise ValueError("choose a candidate_id")
            if all(c.id != candidate_id for c in decision.candidates):
                raise ValueError(f"candidate {candidate_id} isn't one of this decision's")
        elif candidate_id is not None:
            raise ValueError("candidate_id only goes with action 'candidate'")
        decision = store.claim(decision_id, "pending-job")
        payload: dict[str, JsonValue] = {
            "decision_id": decision_id,
            "action": action,
            "candidate_id": candidate_id,
            "by": by,
            "note": note,
        }
        job = self.db.submit_job(decision.profile, "import_decision", payload, decision.run_id)
        store.set_claim(decision_id, job.id)
        await self._wake(decision.profile)
        return job.id

    async def lookup_import(
        self,
        decision_id: str,
        *,
        ids: list[str] | None = None,
        artist: str | None = None,
        album: str | None = None,
    ) -> str:
        """Queue a job that looks the folder up again (IMPORT-UI-9). Returns the job id."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        store = AppImportDecisions(self.db)
        decision = store.claim(decision_id, "pending-job")
        payload: dict[str, JsonValue] = {
            "decision_id": decision_id,
            "ids": list(ids or []),
            "artist": artist,
            "album": album,
        }
        job = self.db.submit_job(decision.profile, "import_lookup", payload, decision.run_id)
        store.set_claim(decision_id, job.id)
        await self._wake(decision.profile)
        return job.id

    async def decide_overdue_imports(self) -> list[str]:
        """Answer every overdue decision with its ``on_timeout`` (IMPORT-UI-10)."""
        decided: list[str] = []
        for decision in AppImportDecisions(self.db).overdue(self._clock()):
            # A duplicate has no as-is answer; it times out to skip, which changes nothing.
            action: ImportAction = decision.on_timeout if decision.kind == "match" else "skip"
            try:
                await self.decide_import(decision.id, action, by="timeout")
            except (ValueError, KeyError) as exc:
                log.info("import decision %s: timeout skipped: %s", decision.id, exc)
                continue
            decided.append(decision.id)
        return decided

    # -- cleanup decisions (S-FILES-CLEANUP) -----------------------------------------

    async def decide_cleanup(
        self,
        decision_id: str,
        decision: CleanupChoice,
        *,
        by: str | None = None,
        note: str | None = None,
    ) -> str | None:
        """Answer a cleanup decision (FILES-CLEANUP-14). ``delete`` queues the worker job
        that cleans the folder and returns its id; ``keep`` deletes nothing and lets the
        held lanes go on at once (returns ``None``). Raises ``KeyError`` (unknown),
        ``CleanupResolvedError`` (decided already), ``CleanupBusyError`` (a job holds
        it) or ``ValueError`` while its run isn't waiting yet."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        store = AppCleanupDecisions(self.db)
        found = store.get(decision_id)
        if found is None:
            raise KeyError(decision_id)
        job = self.db.job_for_run(found.run_id)
        if found.state == "pending" and (job is None or job.state != "waiting_review"):
            state = job.state if job is not None else "missing"
            raise ValueError(f"run {found.run_id} is {state}; the cleanup can't be decided yet")
        claimed = store.claim(decision_id, "pending-job")
        if decision == "delete":
            payload: dict[str, JsonValue] = {"decision_id": decision_id, "by": by, "note": note}
            queued = self.db.submit_job(claimed.profile, "cleanup_apply", payload, claimed.run_id)
            store.set_claim(decision_id, queued.id)
            await self._wake(claimed.profile)
            return queued.id
        claimed.state = "kept"
        claimed.decided_by = by
        claimed.decided_at = self._clock()
        claimed.note = note
        store.release(claimed)
        run_id = claimed.run_id
        data: dict[str, JsonValue] = {
            "decision_id": decision_id,
            "decision": "keep",
            "decided_by": by,
        }
        if note is not None:
            data["note"] = note
        self._emit_run(run_id, "cleanup.resolved", claimed.step_id, data)
        AppLanes(self.db).resolve_hold(run_id, decision_id)
        if settle(self.db, run_id, lambda t, step, d: self._emit_run(run_id, t, step, d)) == (
            "requeued"
        ):
            await self._wake(claimed.profile)
        return None

    # -- cover reviews (S-COVERS-SEARCH) ---------------------------------------------

    async def decide_cover(
        self,
        review_id: str,
        decision: CoverChoice,
        *,
        sha256: str | None = None,
        rationale: str | None = None,
        by: str | None = None,
    ) -> str | None:
        """Answer a cover review (COVERS-SEARCH-21). ``choose`` queues the worker job that
        sets the image ``sha256`` (already in the art store) and returns its id; ``skip``
        lets the album go on without a cover and ``fail`` fails its lane (``None``).
        Raises ``KeyError`` (unknown), ``CoverResolvedError`` (decided already),
        ``CoverBusyError`` (a job holds it) or ``ValueError`` while its run isn't
        waiting yet."""
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        store = AppCoverReviews(self.db)
        found = store.get(review_id)
        if found is None:
            raise KeyError(review_id)
        job = self.db.job_for_run(found.run_id)
        if found.state == "pending" and (job is None or job.state != "waiting_review"):
            state = job.state if job is not None else "missing"
            raise ValueError(f"run {found.run_id} is {state}; the cover can't be decided yet")
        if decision == "choose" and not sha256:
            raise ValueError("choose needs the chosen image")
        claimed = store.claim(review_id, "pending-job")
        if decision == "choose":
            payload: dict[str, JsonValue] = {
                "review_id": review_id,
                "sha256": sha256,
                "rationale": rationale,
                "by": by,
            }
            queued = self.db.submit_job(claimed.profile, "cover_apply", payload, claimed.run_id)
            store.set_claim(review_id, queued.id)
            await self._wake(claimed.profile)
            return queued.id
        run_id = claimed.run_id
        claimed.state = "skipped" if decision == "skip" else "failed"
        claimed.decided_by = by
        claimed.decided_at = self._clock()
        store.release(claimed)
        lanes = AppLanes(self.db)
        if decision == "fail":
            moved = lanes.set_state(run_id, [claimed.album_id], "failed", only_from=("waiting",))
            for key in moved:
                data = {"key": key, "state": "failed", "reason": "no cover (review)"}
                self._emit_run(run_id, "lane.finished", claimed.step_id, dict(data))
        self._emit_run(
            run_id,
            "cover.resolved",
            claimed.step_id,
            {
                "review_id": review_id,
                "album_id": claimed.album_id,
                "decision": decision,
                "decided_by": by,
            },
        )
        lanes.resolve_hold(run_id, review_id)
        if settle(self.db, run_id, lambda t, step, d: self._emit_run(run_id, t, step, d)) == (
            "requeued"
        ):
            await self._wake(claimed.profile)
        return None

    async def decide_overdue_cleanups(self) -> list[str]:
        """Answer every overdue cleanup decision with its ``on_timeout``."""
        decided: list[str] = []
        for decision in AppCleanupDecisions(self.db).overdue(self._clock()):
            try:
                await self.decide_cleanup(decision.id, decision.on_timeout, by="timeout")
            except (ValueError, KeyError) as exc:
                log.info("cleanup decision %s: timeout skipped: %s", decision.id, exc)
                continue
            decided.append(decision.id)
        return decided

    def start(self) -> None:
        """Start background work: the review-gate deadline timer (REVIEW-9)."""
        if self._gate_timer is None:
            self._gate_timer = asyncio.create_task(self._gate_timer_loop())

    async def _gate_timer_loop(self) -> None:
        while not self._shutting_down:
            try:
                await self.decide_overdue_gates()
            except Exception:  # never let the timer die
                log.exception("checking review gate deadlines failed")
            try:
                await self.decide_overdue_imports()
            except Exception:
                log.exception("checking import decision deadlines failed")
            try:
                await self.decide_overdue_cleanups()
            except Exception:
                log.exception("checking cleanup decision deadlines failed")
            wait = self.gate_check_interval
            for upcoming in (
                AppReviewGates(self.db).next_deadline(),
                AppImportDecisions(self.db).next_deadline(),
                AppCleanupDecisions(self.db).next_deadline(),
            ):
                if upcoming is not None:
                    wait = max(0.0, min(wait, (upcoming - self._clock()).total_seconds()))
            self._gates_changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._gates_changed.wait(), wait)

    def _emit_run(
        self, run_id: str, type: str, step_id: str | None, data: dict[str, JsonValue]
    ) -> None:
        seq = self.db.last_seq(run_id) + 1
        self._persist(self._event(run_id, seq, type, data).model_copy(update={"step_id": step_id}))

    async def cancel(self, run_id: str) -> None:
        """Cancel a queued, running or waiting run (WORKER-5, CHANGESET-7). No effect on
        finished runs."""
        job = self.db.job_for_run(run_id)
        if job is None:
            raise KeyError(run_id)
        if self.db.cancel_queued(run_id):
            seq = self.db.last_seq(run_id) + 1
            self._persist(self._event(run_id, seq, "run.finished", {"status": "cancelled"}))
            return
        if self.db.cancel_waiting(run_id):
            self._reject_holds(run_id, job.payload.get("holds"))
            cancel_lanes(self.db, run_id, lambda t, step, d: self._emit_run(run_id, t, step, d))
            AppReviewGates(self.db).cancel_for_run(run_id)  # REVIEW-10
            AppImportDecisions(self.db).cancel_for_run(run_id)  # IMPORT-UI-14
            AppCleanupDecisions(self.db).cancel_for_run(run_id)  # FILES-CLEANUP-14
            AppCoverReviews(self.db).cancel_for_run(run_id)  # COVERS-SEARCH-14
            seq = self.db.last_seq(run_id) + 1
            self._persist(self._event(run_id, seq, "run.finished", {"status": "cancelled"}))
            return
        handle = self._workers.get(job.profile)
        if job.state == "running" and handle is not None:
            await self._send(
                handle, WorkerCommand(kind="cancel_run", profile=job.profile, run_id=run_id)
            )

    def _reject_holds(self, run_id: str, holds: JsonValue) -> None:
        """A run cancelled while waiting rejects its pending ChangeSets (CHANGESET-7)."""
        store = AppChangeSets(self.db)
        for hold in holds if isinstance(holds, list) else []:
            stored = store.get(str(hold))
            if stored is None or stored.state not in ("proposed", "conflicted"):
                continue
            store.set_state(stored.changeset.id, "rejected", resolved_by="cancel")
            seq = self.db.last_seq(run_id) + 1
            event = self._event(run_id, seq, "changeset.rejected", {"changeset_id": str(hold)})
            self._persist(event.model_copy(update={"step_id": stored.changeset.step_id}))

    def worker_pid(self, profile: str) -> int | None:
        handle = self._workers.get(profile)
        return handle.proc.pid if handle is not None else None

    async def wait_idle_exit(self, profile: str) -> None:
        """Test hook: wait until the profile's current worker has exited."""
        handle = self._workers.get(profile)
        if handle is not None:
            await handle.exited.wait()

    async def shutdown(self) -> None:
        """Stop every worker: stop request, then SIGKILL after ``stop_grace`` (WORKER-8)."""
        self._shutting_down = True
        if self._gate_timer is not None:
            self._gate_timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._gate_timer
            self._gate_timer = None
        handles = list(self._workers.values())
        for handle in handles:
            handle.stopping = True
            await self._send(handle, WorkerCommand(kind="stop", profile=handle.profile))
        for handle in handles:
            try:
                await asyncio.wait_for(handle.exited.wait(), self.stop_grace)
            except TimeoutError:
                log.warning("worker %s did not stop in time; killing it", handle.profile)
                handle.kill_reason = "server_shutdown"
                _kill_group(handle.proc.pid)
                await handle.exited.wait()

    # -- workers -------------------------------------------------------------

    async def _wake(self, profile: str) -> None:
        async with self._lock:
            handle = self._workers.get(profile)
            if handle is None or handle.exited.is_set() or handle.stopping:
                handle = await self._spawn(profile)
        await self._send(handle, WorkerCommand(kind="start_run", profile=profile))

    async def _spawn(self, profile: str) -> _Handle:
        argv = self._command(profile)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so kills reach its children
            limit=MAX_MESSAGE,
        )
        loop = asyncio.get_running_loop()
        handle = _Handle(profile, proc, last_seen=loop.time())
        self._workers[profile] = handle
        handle.tasks = [
            asyncio.create_task(self._read(handle)),
            asyncio.create_task(self._log_stderr(handle)),
            asyncio.create_task(self._watchdog(handle)),
        ]
        log.info("started worker for profile %s (pid %s)", profile, proc.pid)
        return handle

    async def _send(self, handle: _Handle, command: WorkerCommand) -> None:
        stdin = handle.proc.stdin
        if stdin is None or handle.exited.is_set():
            return
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(encode(command))
            await stdin.drain()

    async def _read(self, handle: _Handle) -> None:
        assert handle.proc.stdout is not None
        loop = asyncio.get_running_loop()
        try:
            async for line in handle.proc.stdout:
                handle.last_seen = loop.time()
                try:
                    message = WorkerMessage.model_validate_json(line)
                except ValidationError:
                    log.warning("worker %s sent a malformed message", handle.profile)
                    continue
                if message.kind == "ready":
                    handle.ready = True
                elif message.kind == "event" and message.event is not None:
                    self._persist(message.event)
                elif message.kind == "error":
                    log.warning("worker %s: %s", handle.profile, message.message)
                    if not handle.ready:
                        handle.start_error = message.message
        finally:
            await handle.proc.wait()
            await self._on_exit(handle)

    async def _log_stderr(self, handle: _Handle) -> None:
        assert handle.proc.stderr is not None
        async for line in handle.proc.stderr:
            log.info("%s", line.decode(errors="replace").rstrip())

    async def _watchdog(self, handle: _Handle) -> None:
        """Kill a worker that stops sending heartbeats (WORKER-10)."""
        loop = asyncio.get_running_loop()
        while not handle.exited.is_set():
            await asyncio.sleep(self.watchdog_interval)
            if loop.time() - handle.last_seen > self.hung_after and handle.kill_reason is None:
                log.warning("worker %s is not responding; killing it", handle.profile)
                handle.kill_reason = "worker_hung"
                _kill_group(handle.proc.pid)

    async def _on_exit(self, handle: _Handle) -> None:
        code = handle.proc.returncode
        # Whatever it left behind in its process group goes too.
        _kill_group(handle.proc.pid)
        reason: ExitReason = handle.kill_reason or "worker_crashed"
        error = reason
        if not handle.ready:
            reason = "worker_failed_to_start"
            error = handle.start_error or f"worker exited with status {code} before it was ready"
        for job in self.db.fail_jobs(handle.profile, ["running"], reason):
            if job.kind != "run":
                # e.g. a review decision: its run stays waiting_review
                self._job_failed(job, reason)
                continue
            seq = self.db.last_seq(job.run) + 1
            data: dict[str, JsonValue] = {"status": "failed", "reason": reason, "error": error}
            self._persist(self._event(job.run, seq, "run.finished", data))
        if not handle.ready:
            # Don't respawn in a loop: a worker that can't start fails the whole queue.
            for job in self.db.fail_jobs(handle.profile, ["queued"], error):
                if job.kind != "run":
                    self._job_failed(job, error)
                    continue
                data = {"status": "failed", "reason": reason, "error": error}
                seq = self.db.last_seq(job.run) + 1
                self._persist(self._event(job.run, seq, "run.finished", data))
        handle.exited.set()
        if self._workers.get(handle.profile) is handle:
            del self._workers[handle.profile]
        if code not in (0, None) or handle.kill_reason:
            log.warning("worker %s exited (%s, status %s)", handle.profile, reason, code)
        problem = not handle.ready or handle.kill_reason == "worker_hung" or code not in (0, None)
        if problem and not self._shutting_down and reason != "server_shutdown":
            for problem_listener in self._problem_listeners:
                try:
                    problem_listener(handle.profile, reason, error)
                except Exception:
                    log.exception("worker problem listener failed")
        if handle.ready and not self._shutting_down and self.db.jobs(handle.profile, "queued"):
            await self._wake(handle.profile)  # e.g. idle exit raced with a new request
        current = asyncio.current_task()
        for task in handle.tasks:
            if task is not current:
                task.cancel()

    # -- events --------------------------------------------------------------

    def _event(self, run_id: str, seq: int, type: str, data: dict[str, JsonValue]) -> Event:
        return Event(seq=seq, ts=datetime.now(UTC), run_id=run_id, type=type, data=data)

    def _job_failed(self, job: Job, error: str) -> None:
        """A failed job tells its run's stream, or the profile stream for one without a
        run (TAG-GRID-9, REVIEW-QUEUE-10)."""
        stream = job.run_id if job.run_id is not None else profile_stream(job.profile)
        data: dict[str, JsonValue] = {
            "job_id": job.id,
            "kind": job.kind,
            "state": "failed",
            "error": error,
            "result": None,
        }
        self._persist(self._event(stream, self.db.last_seq(stream) + 1, "job.finished", data))

    def _persist(self, event: Event) -> None:
        try:
            self.sink.append(event)  # persisted before anyone sees it
        except ValueError as exc:
            log.warning("dropping out-of-order event: %s", exc)
            return
        if event.type == "run.started":
            self.db.mark_started(event.run_id)
        elif event.type in ("review.requested", "import.requested", "cleanup.requested"):
            self._gates_changed.set()  # a new deadline may be the next one
        for listener in self._listeners:
            listener(event)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
