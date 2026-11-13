"""The read-only worker (WORKER-11): preview and query requests while runs import.

``python -m rifferr.worker --profile P --read-only``. It opens the backend
read-only (BACKEND-13), takes no run, apply or undo jobs and never touches the
job table. Requests arrive as ``request`` commands and are answered, one at a
time, with ``response`` messages; ``cancel_request`` stops one early.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.backends.base import LibraryBackend, QueryError
from rifferr.core.changeset import ChangeSet
from rifferr.core.profile import Profile
from rifferr.core.selection import Selection
from rifferr.core.workflow.errors import ValidationError
from rifferr.core.workflow.interpolation import (
    InterpolationContext,
    InterpolationError,
    interpolate,
)
from rifferr.engine.changesets import current_values
from rifferr.engine.manual import LibraryQuery, ManualChanges, check_manual, library_page
from rifferr.script_store import ScriptPathError, resolve_script
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.models import with_overrides
from rifferr.tagscript.preview import PreviewError, PreviewRequest, PreviewResponse, preview
from rifferr.worker.protocol import WorkerCommand, WorkerMessage

log = logging.getLogger("rifferr.worker")

Send = Callable[[WorkerMessage], None]
Commands = asyncio.Queue[WorkerCommand | None]


class RequestFailed(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _invalid(error: ValidationError) -> PreviewResponse:
    return PreviewResponse(validation_errors=[PreviewError.of(error)])


def run_preview(
    profile: Profile,
    backend: LibraryBackend,
    request: PreviewRequest,
    cancelled: Callable[[], bool] = lambda: False,
) -> PreviewResponse:
    """TAGSCRIPT-20: evaluate the request's script; nothing is proposed or written."""
    if (request.script_text is None) == (request.script is None):
        raise RequestFailed("bad_request", "give exactly one of 'script_text' or 'script'")
    source = request.script or "<editor>"
    if request.script is not None:
        try:
            text = resolve_script(profile.root, request.script).read_text(encoding="utf-8")
        except ScriptPathError as exc:
            raise RequestFailed(exc.code, str(exc)) from None
    else:
        text = request.script_text or ""
    ctx = InterpolationContext(
        vars=profile.vars,
        run={"id": "preview", "workflow": "preview"},
        profile={"name": profile.name},
        steps={},
    )
    try:
        script, errors = parse_script(text, source, lambda v: interpolate(v, ctx))
    except InterpolationError as exc:
        return _invalid(ValidationError(source, "", str(exc)))
    if script is None:
        return PreviewResponse(validation_errors=[PreviewError.of(e) for e in errors])
    try:
        script = with_overrides(script, scope=request.scope)
    except ValueError as exc:
        return _invalid(ValidationError(source, "scope", str(exc)))
    try:
        albums = backend.query(Selection(query=request.query, scope=script.scope))
    except QueryError as exc:
        return _invalid(ValidationError("<query>", "query", str(exc)))
    return preview(script, albums, backend, limit=request.limit, cancelled=cancelled)


def _parse[M: BaseModel](model: type[M], request: dict[str, JsonValue]) -> M:
    try:
        return model.model_validate(request.get("payload") or {})
    except PydanticValidationError as exc:
        raise RequestFailed("bad_request", str(exc)) from None


@dataclass
class _Pending:
    id: str
    request: dict[str, JsonValue]
    cancel: threading.Event = field(default_factory=threading.Event)


class ReadOnlyWorker:
    def __init__(
        self,
        profile: Profile,
        backend: LibraryBackend,
        send: Send,
        *,
        idle_timeout: float | None = None,
    ) -> None:
        if backend.capabilities.apply or backend.capabilities.import_:
            raise ValueError("the read-only worker needs a backend opened read-only")
        self.profile = profile
        self.backend = backend
        self.send = send
        self.idle_timeout = idle_timeout
        self._queue: list[_Pending] = []
        self._current: _Pending | None = None

    async def serve(self, commands: Commands) -> None:
        """Answer requests until stopped, the server goes away, or idle too long."""
        loop = asyncio.get_running_loop()
        self.send(WorkerMessage(kind="ready"))
        task: asyncio.Task[None] | None = None
        idle_since = loop.time()
        get: asyncio.Future[WorkerCommand | None] = asyncio.ensure_future(commands.get())
        stopping = False
        try:
            while True:
                if task is None and self._queue and not stopping:
                    self._current = self._queue.pop(0)
                    task = asyncio.create_task(self._answer(self._current))
                if task is None and stopping:
                    return
                timeout = None
                if task is None and not self._queue and self.idle_timeout is not None:
                    timeout = max(0.0, idle_since + self.idle_timeout - loop.time())
                waiting: set[asyncio.Future[object]] = {get}  # type: ignore[arg-type]
                if task is not None:
                    waiting.add(task)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(
                    waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if not done:
                    log.info("read-only worker idle for %s s, exiting", self.idle_timeout)
                    return
                if task is not None and task in done:
                    task = None
                    self._current = None
                    idle_since = loop.time()
                if get in done:
                    command = get.result()
                    get = asyncio.ensure_future(commands.get())
                    stopping = self._handle(command) or stopping
        finally:
            get.cancel()
            if self._current is not None:
                self._current.cancel.set()
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def _handle(self, command: WorkerCommand | None) -> bool:
        if command is None or command.kind == "stop":
            for p in [*self._queue, *([self._current] if self._current else [])]:
                p.cancel.set()
            return True
        if command.profile != self.profile.name:
            self._error(f"read-only worker for '{self.profile.name}' refused '{command.profile}'")
            return False
        if command.kind == "request" and command.request_id is not None:
            self._queue.append(_Pending(command.request_id, dict(command.request or {})))
        elif command.kind == "cancel_request":
            for p in list(self._queue):
                if p.id == command.request_id:
                    self._queue.remove(p)
                    self._reply(p.id, error=("cancelled", "cancelled"))
            if self._current is not None and self._current.id == command.request_id:
                self._current.cancel.set()
        elif command.kind in ("start_run", "cancel_run"):
            self._error("the read-only worker takes no run, apply or undo jobs")
        return False

    async def _answer(self, pending: _Pending) -> None:
        try:
            result = await asyncio.to_thread(self.handle, pending.request, pending.cancel.is_set)
        except RequestFailed as exc:
            self._reply(pending.id, error=(exc.code, str(exc)))
            return
        except Exception as exc:  # a bug or a broken backend: answer, don't die
            log.exception("request %s failed", pending.id)
            self._reply(pending.id, error=("failed", f"{type(exc).__name__}: {exc}"))
            return
        if pending.cancel.is_set():
            self._reply(pending.id, error=("cancelled", "cancelled"))
        else:
            self._reply(pending.id, result=result)

    def handle(
        self, request: dict[str, JsonValue], cancelled: Callable[[], bool]
    ) -> dict[str, JsonValue]:
        kind = request.get("kind")
        if kind == "tagscript.preview":
            try:
                req = PreviewRequest.model_validate(request.get("payload") or {})
            except PydanticValidationError as exc:
                raise RequestFailed("bad_request", str(exc)) from None
            response = run_preview(self.profile, self.backend, req, cancelled)
            return response.model_dump(mode="json")
        if kind == "library.query":  # TAG-GRID-2
            query = _parse(LibraryQuery, request)
            try:
                page = library_page(self.backend, query)
            except QueryError as exc:
                raise RequestFailed("bad_query", str(exc)) from None
            return page.model_dump(mode="json")
        if kind == "changes.check":  # TAG-GRID-7
            changes = _parse(ManualChanges, request)
            return check_manual(self.backend, changes).model_dump(mode="json")
        if kind == "changes.current":  # REVIEW-QUEUE-3
            changeset = _parse(ChangeSet, request)
            try:
                targets = current_values(self.backend, changeset)
            except QueryError as exc:
                raise RequestFailed("bad_query", str(exc)) from None
            return {"targets": [t.model_dump(mode="json") for t in targets]}
        raise RequestFailed("bad_request", f"unknown request kind {kind!r}")

    def _reply(
        self,
        request_id: str,
        *,
        result: dict[str, JsonValue] | None = None,
        error: tuple[str, str] | None = None,
    ) -> None:
        if error is not None:
            message = WorkerMessage(
                kind="response", request_id=request_id, error_code=error[0], message=error[1]
            )
        else:
            message = WorkerMessage(kind="response", request_id=request_id, result=result)
        self.send(message)

    def _error(self, message: str) -> None:
        self.send(WorkerMessage(kind="error", message=message))
