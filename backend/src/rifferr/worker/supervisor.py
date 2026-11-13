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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import JsonValue, ValidationError

from rifferr.appdb import AppChangeSets, AppDB, AppJournal, AppReviewGates, GateResolvedError
from rifferr.core.review import GateDecision
from rifferr.engine.events import Event, EventSink
from rifferr.engine.undo import run_entries, undoable
from rifferr.trust import workflow_hash
from rifferr.worker.gates import finish_rejected
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

    async def submit_run(
        self,
        profile: str,
        source: str,
        *,
        source_name: str,
        run_vars: dict[str, str] | None = None,
        workflow: str | None = None,
    ) -> str:
        """Queue a run and make sure the profile's worker is up. Returns the run id.

        The workflow *text* is stored with the job, so the worker runs exactly what
        the server checked. Trust is decided here, from the hash of that text
        (SECURITY-10).
        """
        if self._shutting_down:
            raise RuntimeError("server is shutting down")
        payload: dict[str, JsonValue] = {
            "source": source,
            "source_name": source_name,
            "vars": dict(run_vars or {}),
            "trusted": self.db.is_trusted(digest := workflow_hash(source)),
        }
        job = self.db.submit(profile, payload, workflow=workflow, workflow_hash=digest)
        await self._wake(profile)
        return job.run

    async def decide(
        self,
        changeset_id: str,
        *,
        accept: bool,
        accepted: list[int] | None = None,
        by: str | None = None,
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
        cs = stored.changeset
        job = self.db.submit_job(cs.profile, "review_decision", payload, cs.run_id)
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

    # -- review gates (S-REVIEW) ---------------------------------------------------

    async def decide_gate(
        self,
        gate_id: str,
        decision: GateDecision,
        *,
        by: str | None = None,
        note: str | None = None,
    ) -> None:
        """Decide a pending review gate, exactly once (REVIEW-6 .. -8).

        ``continue`` needs no worker job: the run is queued to resume once nothing else
        holds it (REVIEW-12). ``reject`` ends the run here (``on_reject: stop``) or
        queues a worker job that undoes the run's changes first (``undo``). Raises
        ``KeyError`` for an unknown gate, ``GateResolvedError`` when it was already
        decided or cancelled, and ``ValueError`` while its run isn't waiting yet.
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
        decided = gates.decide(
            gate_id, "continued" if decision == "continue" else "rejected", by=by, note=note
        )
        data: dict[str, JsonValue] = {
            "gate_id": gate_id,
            "decision": decision,
            "decided_by": by,
            "decided_at": decided.decided_at.isoformat() if decided.decided_at else None,
        }
        if note is not None:
            data["note"] = note
        self._emit_run(gate.run_id, "review.resolved", gate.step_id, data)
        if decision == "continue":
            if self.db.requeue_if_released(gate.run_id):
                await self._wake(gate.profile)
        elif gate.on_reject == "undo":
            payload: dict[str, JsonValue] = {"gate_id": gate_id, "run_id": gate.run_id, "by": by}
            self.db.submit_job(gate.profile, "review_reject", payload, gate.run_id)
            await self._wake(gate.profile)
        else:
            run_id = gate.run_id
            finish_rejected(self.db, run_id, lambda t, step, d: self._emit_run(run_id, t, step, d))

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
            wait = self.gate_check_interval
            upcoming = AppReviewGates(self.db).next_deadline()
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
            AppReviewGates(self.db).cancel_for_run(run_id)  # REVIEW-10
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
                continue  # e.g. a review decision: its run stays waiting_review
            seq = self.db.last_seq(job.run) + 1
            data: dict[str, JsonValue] = {"status": "failed", "reason": reason, "error": error}
            self._persist(self._event(job.run, seq, "run.finished", data))
        if not handle.ready:
            # Don't respawn in a loop: a worker that can't start fails the whole queue.
            for job in self.db.fail_jobs(handle.profile, ["queued"], error):
                if job.kind != "run":
                    continue
                data = {"status": "failed", "reason": reason, "error": error}
                seq = self.db.last_seq(job.run) + 1
                self._persist(self._event(job.run, seq, "run.finished", data))
        handle.exited.set()
        if self._workers.get(handle.profile) is handle:
            del self._workers[handle.profile]
        if code not in (0, None) or handle.kill_reason:
            log.warning("worker %s exited (%s, status %s)", handle.profile, reason, code)
        if handle.ready and not self._shutting_down and self.db.jobs(handle.profile, "queued"):
            await self._wake(handle.profile)  # e.g. idle exit raced with a new request
        current = asyncio.current_task()
        for task in handle.tasks:
            if task is not current:
                task.cancel()

    # -- events --------------------------------------------------------------

    def _event(self, run_id: str, seq: int, type: str, data: dict[str, JsonValue]) -> Event:
        return Event(seq=seq, ts=datetime.now(UTC), run_id=run_id, type=type, data=data)

    def _persist(self, event: Event) -> None:
        try:
            self.sink.append(event)  # persisted before anyone sees it
        except ValueError as exc:
            log.warning("dropping out-of-order event: %s", exc)
            return
        if event.type == "run.started":
            self.db.mark_started(event.run_id)
        elif event.type == "review.requested":
            self._gates_changed.set()  # a new deadline may be the next one
        for listener in self._listeners:
            listener(event)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
