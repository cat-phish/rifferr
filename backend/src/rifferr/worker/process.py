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
import sys
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import JsonValue, ValidationError

from rifferr.appdb import AppChangeSets, AppDB, AppJournal, Job
from rifferr.backends.base import LibraryBackend
from rifferr.backends.loader import load_backend
from rifferr.core.ids import new_ulid
from rifferr.core.profile import Profile, Redactor
from rifferr.core.workflow import WorkflowLoadError, load_workflow
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import (
    Engine,
    Event,
    FailedTarget,
    Run,
    RunState,
    RunValidationError,
    SecretAccess,
    StepRecord,
    StepTypeRegistry,
)
from rifferr.engine.changesets import apply_changeset, reject_changeset
from rifferr.profile_store import app_db_path, config_root, load_profile
from rifferr.steps.registry import load_step_types
from rifferr.worker.protocol import HEARTBEAT_INTERVAL, WorkerCommand, WorkerMessage, encode

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
HOLDING = frozenset({"proposed", "conflicted"})


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
    ) -> None:
        self.profile = profile
        self.db = db
        self.registry = registry
        self.backend = backend
        self.send = send
        self.secrets = secrets
        self.idle_timeout = idle_timeout
        self.cancel_grace = cancel_grace
        self._engine: Engine | None = None
        self._run: Run | None = None
        self._sent: dict[str, int] = {}
        self.journal = AppJournal(db)
        self.changesets = AppChangeSets(db)

    async def serve(self, commands: Commands) -> None:
        """Run jobs until stopped, the server goes away, or idle too long (WORKER-7)."""
        loop = asyncio.get_running_loop()
        self.send(WorkerMessage(kind="ready"))
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

    def _next_seq(self, run_id: str) -> int:
        return max(self.db.last_seq(run_id), self._sent.get(run_id, 0)) + 1

    async def _run_job(self, job: Job) -> None:
        if job.kind == "review_decision":
            self._decide(job)
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
                doc=doc,
                redactor=Redactor([]),
                trusted=payload.get("trusted") is True,
                work_dir=self.profile.root,
                secrets=self.secrets,
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
                self.db.suspend(job.run, run.suspended_after, run.holds)
            else:
                self.db.finish(job.run, run.state.value)  # type: ignore[arg-type]
        except Exception as exc:  # an engine bug: still end the run cleanly
            log.exception("run %s crashed", run.id)
            self.db.finish(job.run, "failed", f"{type(exc).__name__}: {exc}")
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
            if isinstance(after, str) and isinstance(holds, list):
                self.db.suspend(event.run_id, after, [str(h) for h in holds])

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
            self.db.finish_job(job.id, "failed", f"no ChangeSet {changeset_id}")
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
        except Exception as exc:
            log.warning("review decision on %s failed: %s", changeset_id, exc)
            self.db.finish_job(job.id, "failed", f"{type(exc).__name__}: {exc}")
            return
        self.db.finish_job(job.id, "succeeded")
        if cs.run_id is not None:
            self._resume_if_released(cs.run_id)

    def _resume_if_released(self, run_id: str) -> None:
        run_job = self.db.job_for_run(run_id)
        if run_job is None or run_job.state != "waiting_review":
            return
        holds = run_job.payload.get("holds") or []
        assert isinstance(holds, list)
        for hold in holds:
            stored = self.changesets.get(str(hold))
            if stored is not None and stored.state in HOLDING:
                return
        self.db.requeue(run_id)  # the serve loop claims it next (RUN-14)

    def _changeset_emitter(
        self, run_id: str | None, step_id: str | None
    ) -> Callable[[str, dict[str, JsonValue]], None]:
        def emit(type: str, data: dict[str, JsonValue]) -> None:
            if run_id is None:
                # Manual ChangeSets get a profile stream with F-REVIEW-QUEUE.
                log.info("%s %s", type, data.get("changeset_id"))
                return
            seq = self._next_seq(run_id)
            self._sent[run_id] = seq
            event = Event(
                seq=seq, ts=datetime.now(UTC), run_id=run_id, type=type, step_id=step_id, data=data
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
        elif e.type == "run.step.finished":
            data = e.data
            outputs = data.get("outputs") or {}
            failed = data.get("failed_targets") or []
            assert isinstance(outputs, dict)
            assert isinstance(failed, list)
            error = data.get("error")
            records[e.step_id] = StepRecord(
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
            )
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
    args = parser.parse_args(argv)
    channel = _claim_channel()
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format=f"[worker {args.profile}] %(message)s"
    )
    stop = threading.Event()
    threading.Thread(
        target=_heartbeat, args=(channel.send, heartbeat_interval, stop), daemon=True
    ).start()
    try:
        profile = load_profile(config_root(), args.profile)
        backend = (backend_factory or load_backend)(profile)
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
        )
        await worker.serve(queue)

    try:
        asyncio.run(serve())
    finally:
        stop.set()
        db.close()
    return 0
