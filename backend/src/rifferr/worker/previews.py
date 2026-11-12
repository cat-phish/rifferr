"""Server side of WORKER-11: one read-only worker per profile, for previews.

Requests are not stored: they live in memory while the worker answers and are
lost if it crashes (``PreviewUnavailable``). The same heartbeat, hang and
shutdown rules as the main worker apply (WORKER-7, -8, -10).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import JsonValue, ValidationError

from rifferr.core.ids import new_ulid
from rifferr.worker.protocol import HUNG_AFTER, MAX_MESSAGE, WorkerCommand, WorkerMessage, encode
from rifferr.worker.supervisor import STOP_GRACE, WATCHDOG_INTERVAL, _kill_group  # pyright: ignore[reportPrivateUsage]

log = logging.getLogger("rifferr.worker")

Command = Callable[[str], list[str]]


class PreviewUnavailable(Exception):
    """The read-only worker couldn't start or went away (``503 preview_unavailable``)."""


class PreviewTimeout(Exception):
    """No answer in time (``504 preview_timeout``)."""


class PreviewSuperseded(Exception):
    """A newer request from the same client replaced this one (``409 superseded``)."""


class PreviewFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass
class _Request:
    id: str
    client: str | None
    future: asyncio.Future[dict[str, JsonValue]]


@dataclass
class _Handle:
    profile: str
    proc: asyncio.subprocess.Process
    last_seen: float
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    start_error: str | None = None
    kill_reason: str | None = None
    pending: dict[str, _Request] = field(default_factory=dict[str, _Request])
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    exited: asyncio.Event = field(default_factory=asyncio.Event)


class PreviewSupervisor:
    def __init__(
        self,
        *,
        command: Command | None = None,
        hung_after: float = HUNG_AFTER,
        stop_grace: float = STOP_GRACE,
        watchdog_interval: float = WATCHDOG_INTERVAL,
        start_timeout: float = 30.0,
    ) -> None:
        self._command = command or self.default_command
        self.hung_after = hung_after
        self.stop_grace = stop_grace
        self.watchdog_interval = watchdog_interval
        self.start_timeout = start_timeout
        self._workers: dict[str, _Handle] = {}
        self._lock = asyncio.Lock()
        self._shutting_down = False

    @staticmethod
    def default_command(profile: str) -> list[str]:
        return [sys.executable, "-m", "rifferr.worker", "--profile", profile, "--read-only"]

    def worker_pid(self, profile: str) -> int | None:
        handle = self._workers.get(profile)
        return handle.proc.pid if handle is not None and not handle.exited.is_set() else None

    async def request(
        self,
        profile: str,
        request: dict[str, JsonValue],
        *,
        timeout: float,
        client: str | None = None,
    ) -> dict[str, JsonValue]:
        """Send ``request`` to the profile's read-only worker and wait for the answer."""
        if self._shutting_down:
            raise PreviewUnavailable("server is shutting down")
        handle = await self._worker(profile)
        if client is not None:
            for older in list(handle.pending.values()):
                if older.client == client:
                    self._settle(handle, older, PreviewSuperseded("a newer preview replaced it"))
                    await self._send(handle, "cancel_request", older.id)
        loop = asyncio.get_running_loop()
        req = _Request(new_ulid(), client, loop.create_future())
        handle.pending[req.id] = req
        await self._send(handle, "request", req.id, request)
        try:
            return await asyncio.wait_for(asyncio.shield(req.future), timeout)
        except TimeoutError:
            self._settle(handle, req, PreviewTimeout(f"no answer within {timeout:g} s"))
            await self._send(handle, "cancel_request", req.id)
            raise PreviewTimeout(f"no answer within {timeout:g} s") from None
        finally:
            handle.pending.pop(req.id, None)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for handle in list(self._workers.values()):
            await self._send(handle, "stop")
            if handle.proc.stdin is not None:
                handle.proc.stdin.close()
        for handle in list(self._workers.values()):
            try:
                await asyncio.wait_for(handle.exited.wait(), self.stop_grace)
            except TimeoutError:
                handle.kill_reason = "server_shutdown"
                _kill_group(handle.proc.pid)
                await handle.exited.wait()

    # -- workers -------------------------------------------------------------

    async def _worker(self, profile: str) -> _Handle:
        async with self._lock:
            handle = self._workers.get(profile)
            if handle is None or handle.exited.is_set():
                handle = await self._spawn(profile)
        if not handle.ready.is_set():
            waiter = asyncio.ensure_future(handle.ready.wait())
            gone = asyncio.ensure_future(handle.exited.wait())
            done, _ = await asyncio.wait(
                {waiter, gone}, timeout=self.start_timeout, return_when=asyncio.FIRST_COMPLETED
            )
            waiter.cancel()
            gone.cancel()
            if not handle.ready.is_set():
                if not done:
                    _kill_group(handle.proc.pid)
                raise PreviewUnavailable(
                    handle.start_error or "the profile's read-only worker didn't start"
                )
        return handle

    async def _spawn(self, profile: str) -> _Handle:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command(profile),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=MAX_MESSAGE,
            )
        except OSError as exc:
            raise PreviewUnavailable(f"can't start the read-only worker: {exc}") from exc
        handle = _Handle(profile, proc, last_seen=asyncio.get_running_loop().time())
        self._workers[profile] = handle
        handle.tasks = [
            asyncio.create_task(self._read(handle)),
            asyncio.create_task(self._log_stderr(handle)),
            asyncio.create_task(self._watchdog(handle)),
        ]
        log.info("started read-only worker for profile %s (pid %s)", profile, proc.pid)
        return handle

    async def _send(
        self,
        handle: _Handle,
        kind: str,
        request_id: str | None = None,
        request: dict[str, JsonValue] | None = None,
    ) -> None:
        stdin = handle.proc.stdin
        if stdin is None or handle.exited.is_set():
            return
        command = WorkerCommand(
            kind=kind,  # type: ignore[arg-type]
            profile=handle.profile,
            request_id=request_id,
            request=request,
        )
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.write(encode(command))
            await stdin.drain()

    @staticmethod
    def _settle(handle: _Handle, req: _Request, exc: Exception) -> None:
        handle.pending.pop(req.id, None)
        if not req.future.done():
            req.future.set_exception(exc)
            req.future.exception()  # retrieved: no "never retrieved" warning

    async def _read(self, handle: _Handle) -> None:
        assert handle.proc.stdout is not None
        loop = asyncio.get_running_loop()
        try:
            async for line in handle.proc.stdout:
                handle.last_seen = loop.time()
                try:
                    message = WorkerMessage.model_validate_json(line)
                except ValidationError:
                    log.warning("read-only worker %s sent a malformed message", handle.profile)
                    continue
                if message.kind == "ready":
                    handle.ready.set()
                elif message.kind == "response" and message.request_id is not None:
                    req = handle.pending.get(message.request_id)
                    if req is None or req.future.done():
                        continue
                    if message.result is not None:
                        req.future.set_result(message.result)
                    else:
                        code = message.error_code or "failed"
                        self._settle(handle, req, PreviewFailed(code, message.message or code))
                elif message.kind == "error":
                    log.warning("read-only worker %s: %s", handle.profile, message.message)
                    if not handle.ready.is_set():
                        handle.start_error = message.message
        finally:
            await handle.proc.wait()
            self._on_exit(handle)

    async def _log_stderr(self, handle: _Handle) -> None:
        assert handle.proc.stderr is not None
        async for line in handle.proc.stderr:
            log.info("%s", line.decode(errors="replace").rstrip())

    async def _watchdog(self, handle: _Handle) -> None:
        loop = asyncio.get_running_loop()
        while not handle.exited.is_set():
            await asyncio.sleep(self.watchdog_interval)
            if loop.time() - handle.last_seen > self.hung_after and handle.kill_reason is None:
                log.warning("read-only worker %s is not responding; killing it", handle.profile)
                handle.kill_reason = "worker_hung"
                _kill_group(handle.proc.pid)

    def _on_exit(self, handle: _Handle) -> None:
        _kill_group(handle.proc.pid)
        reason = handle.kill_reason or "the read-only worker exited"
        for req in list(handle.pending.values()):
            self._settle(handle, req, PreviewUnavailable(reason))
        handle.exited.set()
        if self._workers.get(handle.profile) is handle:
            del self._workers[handle.profile]
        current = asyncio.current_task()
        for task in handle.tasks:
            if task is not current:
                task.cancel()
