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
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import ValidationError

from rifferr.appdb import AppDB, Job
from rifferr.backends.base import LibraryBackend
from rifferr.backends.loader import load_backend
from rifferr.core.profile import Profile, Redactor
from rifferr.core.workflow import WorkflowLoadError, load_workflow
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import Engine, Event, Run, RunValidationError, SecretAccess, StepTypeRegistry
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


class _ForwardSink:
    """Engine event sink that hands events to the server, which persists them (WORKER-4)."""

    def __init__(self, send: Send) -> None:
        self._send = send

    def append(self, event: Event) -> None:
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

    async def _run_job(self, job: Job) -> None:
        payload = job.payload
        source = str(payload.get("source", ""))
        source_name = str(payload.get("source_name", "<workflow>"))
        run_vars = payload.get("vars") or {}
        assert isinstance(run_vars, dict)
        engine = Engine(
            self.registry,
            _ForwardSink(self.send),
            new_id=lambda: job.run_id,
            cancel_grace=self.cancel_grace,
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
            )
        except (WorkflowLoadError, RunValidationError) as exc:
            self._fail_unstarted(job, str(exc))
            return
        except Exception as exc:  # a bug (e.g. in a step's parameter model)
            log.exception("run %s could not be created", job.run_id)
            self._fail_unstarted(job, f"internal error: {type(exc).__name__}: {exc}")
            return
        self._engine, self._run = engine, run
        try:
            await engine.execute(run)
            self.db.finish(job.run_id, run.state.value)  # type: ignore[arg-type]
        except Exception as exc:  # an engine bug: still end the run cleanly
            log.exception("run %s crashed", run.id)
            self.db.finish(job.run_id, "failed", f"{type(exc).__name__}: {exc}")
            if not run.finished:
                self._send_finished(run.id, run.last_seq + 1, f"internal error: {exc}")
        finally:
            self._engine = self._run = None

    def _fail_unstarted(self, job: Job, error: str) -> None:
        self.db.finish(job.run_id, "failed", error)
        self._send_finished(job.run_id, 1, error)

    def _send_finished(self, run_id: str, seq: int, error: str) -> None:
        event = Event(
            seq=seq,
            ts=datetime.now(UTC),
            run_id=run_id,
            type="run.finished",
            data={"status": "failed", "error": error},
        )
        self.send(WorkerMessage(kind="event", event=event))


# -- process ------------------------------------------------------------------


def _read_commands(stdin: BinaryIO, loop: asyncio.AbstractEventLoop, queue: Commands) -> None:
    def put(item: WorkerCommand | None) -> None:
        with contextlib.suppress(RuntimeError):  # loop already closed
            loop.call_soon_threadsafe(queue.put_nowait, item)

    for line in stdin:
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
            target=_read_commands, args=(sys.stdin.buffer, loop, queue), daemon=True
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
