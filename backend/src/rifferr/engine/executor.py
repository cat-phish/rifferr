"""The run executor (C-RUN).

Pure apart from injected interfaces: step registry, event sink, clock, id
factory, backend and redactor (RUN-9). M0 runs steps sequentially over the whole
run; lanes arrive in M2.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.backends.base import LibraryBackend
from rifferr.core.ids import new_ulid
from rifferr.core.profile import Redactor, merged_vars
from rifferr.core.selection import Selection
from rifferr.core.workflow import (
    InterpolationContext,
    InterpolationError,
    RoundTripDoc,
    StepSpec,
    ValidationError,
    Workflow,
    interpolate,
    validate,
)
from rifferr.engine.events import Event, EventSink
from rifferr.engine.run import Run, RunState, Scalar, StepRecord
from rifferr.engine.steps import StepContext, StepFailure, StepTypeRegistry

DEFAULT_CANCEL_GRACE = 5.0


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class RunValidationError(Exception):
    """The workflow failed validation; no run was created (RUN-10)."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


Listener = Callable[[Event], None]


class _RunState:
    """Engine-side bookkeeping for one run."""

    def __init__(self, run: Run, wf: Workflow, backend: LibraryBackend, redactor: Redactor) -> None:
        self.run = run
        self.workflow = wf
        self.backend = backend
        self.redactor = redactor
        self.cancel = asyncio.Event()


class Engine:
    def __init__(
        self,
        registry: StepTypeRegistry,
        sink: EventSink,
        *,
        clock: Clock | None = None,
        new_id: Callable[[], str] = new_ulid,
        cancel_grace: float = DEFAULT_CANCEL_GRACE,
    ) -> None:
        self.registry = registry
        self.sink = sink
        self.clock = clock or SystemClock()
        self._new_id = new_id
        self.cancel_grace = cancel_grace
        self.runs: dict[str, Run] = {}
        self._state: dict[str, _RunState] = {}
        self._listeners: list[Listener] = []

    # -- public API ----------------------------------------------------------

    def subscribe(self, listener: Listener) -> None:
        """Called with each event after it has been persisted (RUN-7)."""
        self._listeners.append(listener)

    def create_run(
        self,
        wf: Workflow,
        source: bytes,
        *,
        profile_name: str,
        profile_vars: Mapping[str, str],
        backend: LibraryBackend,
        run_vars: Mapping[str, str] | None = None,
        doc: RoundTripDoc | None = None,
        redactor: Redactor | None = None,
    ) -> Run:
        """Validate and create a queued run (RUN-10, RUN-11)."""
        supplied = {**profile_vars, **(run_vars or {})}
        errors = validate(wf, self.registry, backend.capabilities, supplied, doc=doc)
        if errors:
            raise RunValidationError(errors)
        run = Run(
            id=self._new_id(),
            workflow=wf.name,
            workflow_hash=hashlib.sha256(source).hexdigest(),
            profile=profile_name,
            created_at=self.clock.now(),
            steps=[StepRecord(id=s.id, uses=s.uses) for s in wf.steps],
            vars=merged_vars(supplied, wf.vars),
        )
        self.runs[run.id] = run
        self._state[run.id] = _RunState(run, wf, backend, redactor or Redactor([]))
        return run

    def cancel(self, run_id: str) -> None:
        """Request cancellation. No effect on a finished run (RUN-8)."""
        state = self._state[run_id]
        if not state.run.finished:
            state.cancel.set()

    async def execute(self, run: Run) -> None:
        state = self._state[run.id]
        if state.cancel.is_set():
            run.transition(RunState.cancelled)
            for record in run.steps:
                record.status = "skipped"
            self._finish(run)
            return
        run.transition(RunState.running)
        run.started_at = self.clock.now()
        self._emit(run, "run.started", None, {"workflow": run.workflow, "profile": run.profile})
        outcome = RunState.succeeded
        for index, (spec, record) in enumerate(zip(state.workflow.steps, run.steps, strict=True)):
            stop = await self._execute_step(state, spec, record)
            if record.status == "cancelled":
                outcome = RunState.cancelled
            elif stop:
                outcome = RunState.failed
            if outcome is not RunState.succeeded:
                for later in run.steps[index + 1 :]:
                    later.status = "skipped"
                break
        run.transition(outcome)
        self._finish(run)

    # -- steps ---------------------------------------------------------------

    async def _execute_step(self, state: _RunState, spec: StepSpec, record: StepRecord) -> bool:
        """Run one step; return True when the run must stop because of a failure."""
        run = state.run
        record.status = "running"
        record.started_at = self.clock.now()
        self._emit(run, "run.step.started", spec.id, {"uses": spec.uses})
        ctx: StepContext | None = None
        try:
            params = self._prepare_params(run, spec)
            step_type = self.registry.step_type(spec.uses)
            assert step_type is not None, "validated before the run was created"
            ctx = StepContext(
                run_id=run.id,
                step=spec,
                params=params,
                backend=state.backend,
                selection=Selection(query=spec.when or "", run_id=run.id),
                emit=lambda type, data: self._emit_from_step(state, spec.id, type, data),
                is_cancelled=state.cancel.is_set,
            )
            result = await self._run_cancellable(state, step_type.run(ctx))
            if state.cancel.is_set():
                record.status = "cancelled"
            else:
                record.outputs = dict(result.outputs) if result else {}
                record.status = "failed" if ctx.failed_targets else "succeeded"
        except asyncio.CancelledError:
            record.status = "cancelled"
        except StepFailure as exc:
            record.status = "cancelled" if state.cancel.is_set() else "failed"
            record.error = str(exc)
        except Exception as exc:  # a bug in a step: still a step failure, never a crash
            record.status = "cancelled" if state.cancel.is_set() else "failed"
            record.error = f"{type(exc).__name__}: {exc}"
        if ctx is not None:
            record.failed_targets = list(ctx.failed_targets)
        record.finished_at = self.clock.now()
        data: dict[str, JsonValue] = {
            "status": record.status,
            "outputs": cast(dict[str, JsonValue], dict(record.outputs)),
            "failed_targets": [
                {"target": f.target, "message": f.message} for f in record.failed_targets
            ],
        }
        if record.error is not None:
            data["error"] = state.redactor.redact(record.error)
        self._emit(run, "run.step.finished", spec.id, data)
        return record.status == "failed" and spec.on_error == "stop"

    async def _run_cancellable(self, state: _RunState, coro: Any) -> Any:
        task: asyncio.Task[Any] = asyncio.ensure_future(coro)
        cancel_wait = asyncio.ensure_future(state.cancel.wait())
        try:
            await asyncio.wait({task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
            if not task.done():
                # Give the step a chance to notice ctx.cancelled and return cleanly.
                await asyncio.wait({task}, timeout=self.cancel_grace)
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return None
            return task.result()
        finally:
            cancel_wait.cancel()

    def _prepare_params(self, run: Run, spec: StepSpec) -> BaseModel:
        ctx = InterpolationContext(
            vars=run.vars,
            run={"id": run.id, "workflow": run.workflow},
            profile={"name": run.profile},
            steps={r.id: r.outputs for r in run.steps if r.status in ("succeeded", "failed")},
        )
        try:
            values = _interpolate_values(spec.with_, ctx)
        except InterpolationError as exc:
            raise StepFailure(str(exc)) from exc
        step_type = self.registry.step_type(spec.uses)
        assert step_type is not None
        try:
            return step_type.params_model.model_validate(values)
        except PydanticValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
            )
            raise StepFailure(f"invalid parameters after interpolation: {problems}") from exc

    # -- events --------------------------------------------------------------

    def _emit_from_step(
        self, state: _RunState, step_id: str, type: str, data: dict[str, JsonValue]
    ) -> None:
        self._emit(state.run, type, step_id, _redact(data, state.redactor))

    def _emit(self, run: Run, type: str, step_id: str | None, data: dict[str, JsonValue]) -> None:
        run.last_seq += 1
        event = Event(
            seq=run.last_seq,
            ts=self.clock.now(),
            run_id=run.id,
            type=type,
            step_id=step_id,
            data=data,
        )
        self.sink.append(event)  # persisted before anyone sees it
        for listener in self._listeners:
            listener(event)

    def _finish(self, run: Run) -> None:
        run.finished_at = self.clock.now()
        self._emit(run, "run.finished", None, {"status": run.state.value})


def _interpolate_values(value: Any, ctx: InterpolationContext) -> Any:
    if isinstance(value, str):
        return interpolate(value, ctx)
    if isinstance(value, dict):
        items = cast(dict[str, Any], value).items()
        return {k: _interpolate_values(v, ctx) for k, v in items}
    if isinstance(value, list):
        return [_interpolate_values(v, ctx) for v in cast(list[Any], value)]
    return value


def _redact(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, dict):
        items = cast(dict[str, Any], value).items()
        return {k: _redact(v, redactor) for k, v in items}
    if isinstance(value, list):
        return [_redact(v, redactor) for v in cast(list[Any], value)]
    return value


__all__ = ["Clock", "Engine", "RunValidationError", "Scalar", "SystemClock"]
