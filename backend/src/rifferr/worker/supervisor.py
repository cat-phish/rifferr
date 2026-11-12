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
from typing import Literal

from pydantic import JsonValue, ValidationError

from rifferr.appdb import AppDB
from rifferr.engine.events import Event, EventSink
from rifferr.trust import workflow_hash
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
            "trusted": self.db.is_trusted(workflow_hash(source)),
        }
        job = self.db.submit(profile, payload)
        await self._wake(profile)
        return job.run_id

    async def cancel(self, run_id: str) -> None:
        """Cancel a queued or running run (WORKER-5). No effect on finished runs."""
        job = self.db.job_for_run(run_id)
        if job is None:
            raise KeyError(run_id)
        if self.db.cancel_queued(run_id):
            self._persist(self._event(run_id, 1, "run.finished", {"status": "cancelled"}))
            return
        handle = self._workers.get(job.profile)
        if job.state == "running" and handle is not None:
            await self._send(
                handle, WorkerCommand(kind="cancel_run", profile=job.profile, run_id=run_id)
            )

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
            seq = self.db.last_seq(job.run_id) + 1
            data: dict[str, JsonValue] = {"status": "failed", "reason": reason, "error": error}
            self._persist(self._event(job.run_id, seq, "run.finished", data))
        if not handle.ready:
            # Don't respawn in a loop: a worker that can't start fails the whole queue.
            for job in self.db.fail_jobs(handle.profile, ["queued"], error):
                data = {"status": "failed", "reason": reason, "error": error}
                self._persist(self._event(job.run_id, 1, "run.finished", data))
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
        for listener in self._listeners:
            listener(event)


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)
