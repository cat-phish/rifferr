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
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import BaseModel, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.backends.base import CapabilityError, LibraryBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetProposal,
    ChangeSource,
    JournalEntry,
    TargetRef,
)
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
from rifferr.engine.changesets import (
    ChangeSetStore,
    InMemoryChangeSetStore,
    ProposalError,
    SnapshotIndex,
    apply_changeset,
    build_changesets,
    new_changeset,
    proposed_event,
)
from rifferr.engine.events import Event, EventSink
from rifferr.engine.journal import InMemoryJournal, Journal
from rifferr.engine.run import Run, RunState, Scalar, StepRecord
from rifferr.engine.steps import (
    NoSecrets,
    SecretAccess,
    StepContext,
    StepFailure,
    StepRefused,
    StepTypeRegistry,
)

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

    def __init__(
        self,
        run: Run,
        wf: Workflow,
        backend: LibraryBackend,
        redactor: Redactor,
        work_dir: Path | None,
        secrets: SecretAccess,
        profile_dir: Path | None = None,
    ) -> None:
        self.run = run
        self.profile_dir = profile_dir
        self.workflow = wf
        self.backend = backend
        self.redactor = redactor
        self.work_dir = work_dir
        self.secrets = secrets
        self.cancel = asyncio.Event()
        self.holds: dict[str, list[str]] = {}
        """step id -> ChangeSet ids it proposed that wait for review (CHANGESET-7)."""


class Engine:
    def __init__(
        self,
        registry: StepTypeRegistry,
        sink: EventSink,
        *,
        clock: Clock | None = None,
        new_id: Callable[[], str] = new_ulid,
        cancel_grace: float = DEFAULT_CANCEL_GRACE,
        journal: Journal | None = None,
        changesets: ChangeSetStore | None = None,
    ) -> None:
        self.registry = registry
        self.sink = sink
        self.journal: Journal = journal or InMemoryJournal()
        self.changesets: ChangeSetStore = changesets or InMemoryChangeSetStore()
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
        trusted: bool = False,
        work_dir: Path | None = None,
        secrets: SecretAccess | None = None,
        last_seq: int = 0,
        profile_dir: Path | None = None,
    ) -> Run:
        """Validate and create a queued run (RUN-10, RUN-11).

        ``trusted``: may this run execute shell/python steps (decided by F-SECURITY).
        ``work_dir``: default working directory for steps without a target (the profile dir).
        ``last_seq``: events already stored for this run (a resumed run, RUN-14).
        ``profile_dir``: the profile's folder, for steps that read files from it.
        """
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
            trusted=trusted,
            last_seq=last_seq,
        )
        self.runs[run.id] = run
        self._state[run.id] = _RunState(
            run,
            wf,
            backend,
            redactor or Redactor([]),
            work_dir,
            secrets or NoSecrets(),
            profile_dir,
        )
        return run

    def cancel(self, run_id: str) -> None:
        """Request cancellation. No effect on a finished run (RUN-8)."""
        state = self._state[run_id]
        if not state.run.finished:
            state.cancel.set()

    def restore_steps(self, run: Run, records: Mapping[str, StepRecord]) -> None:
        """Put back the records of steps a resumed run already finished (RUN-14)."""
        for i, record in enumerate(run.steps):
            if record.id in records:
                run.steps[i] = records[record.id]

    async def execute(self, run: Run, *, resume_after: str | None = None) -> None:
        """Run the steps; with ``resume_after``, continue after that step (RUN-14).

        Returns with the run finished, or ``waiting_review`` when a step's ChangeSets
        wait for review (CHANGESET-7).
        """
        state = self._state[run.id]
        start = 0
        if resume_after is not None:
            ids = [r.id for r in run.steps]
            if resume_after not in ids:
                raise ValueError(f"run {run.id} has no step {resume_after!r} to resume after")
            start = ids.index(resume_after) + 1
        if state.cancel.is_set():
            run.transition(RunState.cancelled)
            for record in run.steps[start:]:
                record.status = "skipped"
            self._finish(run)
            return
        run.transition(RunState.running)
        run.started_at = self.clock.now()
        if resume_after is None:
            self._emit(run, "run.started", None, {"workflow": run.workflow, "profile": run.profile})
        else:
            self._emit(run, "run.resumed", None, {"after": resume_after})
        outcome = RunState.succeeded
        steps = list(zip(state.workflow.steps, run.steps, strict=True))
        for index in range(start, len(steps)):
            spec, record = steps[index]
            stop = await self._execute_step(state, spec, record)
            if record.status == "cancelled":
                outcome = RunState.cancelled
            elif stop:
                outcome = RunState.failed
            if outcome is not RunState.succeeded:
                for later in run.steps[index + 1 :]:
                    later.status = "skipped"
                break
            holds = state.holds.pop(spec.id, [])
            if holds:
                self._suspend(run, spec.id, holds)
                return
        run.transition(outcome)
        self._finish(run)

    def _suspend(self, run: Run, step_id: str, holds: list[str]) -> None:
        run.transition(RunState.waiting_review)
        run.suspended_after = step_id
        run.holds = list(holds)
        self._emit(
            run,
            "run.waiting_review",
            None,
            {"after": step_id, "changesets": cast(list[JsonValue], list(holds)), "gates": []},
        )

    # -- steps ---------------------------------------------------------------

    async def _execute_step(self, state: _RunState, spec: StepSpec, record: StepRecord) -> bool:
        """Run one step; return True when the run must stop because of a failure."""
        run = state.run
        record.status = "running"
        record.started_at = self.clock.now()
        self._emit(run, "run.step.started", spec.id, {"uses": spec.uses})
        ctx: StepContext | None = None
        refused = False
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
                profile=run.profile,
                vars=run.vars,
                work_dir=state.work_dir,
                trusted=run.trusted,
                secrets=state.secrets,
                on_secret=state.redactor.add,
                propose=lambda proposal, current, snapshots: self._propose(
                    state, spec, proposal, current, snapshots
                ),
                profile_dir=state.profile_dir,
                interpolate=lambda value: self._interpolate_text(run, value),
            )
            result = await self._run_cancellable(state, step_type.run(ctx))
            if state.cancel.is_set():
                record.status = "cancelled"
            else:
                record.outputs = dict(result.outputs) if result else {}
                record.status = "failed" if ctx.failed_targets else "succeeded"
        except asyncio.CancelledError:
            record.status = "cancelled"
        except StepRefused as exc:
            record.status = "failed"
            record.error = str(exc)
            refused = True
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
        if refused:
            data["refused"] = True  # e.g. needs trust; lets clients give a hint (CLI-10)
        self._emit(run, "run.step.finished", spec.id, data)
        return refused or (record.status == "failed" and spec.on_error == "stop")

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

    @staticmethod
    def _interpolation_context(run: Run) -> InterpolationContext:
        return InterpolationContext(
            vars=run.vars,
            run={"id": run.id, "workflow": run.workflow},
            profile={"name": run.profile},
            steps={r.id: r.outputs for r in run.steps if r.status in ("succeeded", "failed")},
        )

    def _interpolate_text(self, run: Run, value: str) -> str:
        try:
            return interpolate(value, self._interpolation_context(run))
        except InterpolationError as exc:
            raise StepFailure(str(exc)) from exc

    def _prepare_params(self, run: Run, spec: StepSpec) -> BaseModel:
        ctx = self._interpolation_context(run)
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

    # -- ChangeSets (C-CHANGESET) ----------------------------------------------

    def _propose(
        self,
        state: _RunState,
        spec: StepSpec,
        proposal: ChangeSetProposal,
        current: TargetRef | None,
        snapshots: SnapshotIndex,
    ) -> list[ChangeSet]:
        run = state.run
        step_type = self.registry.step_type(spec.uses)
        force_review = bool(getattr(step_type, "force_review", False))
        source = ChangeSource(kind="step", name=spec.uses)

        def make(targets: Any, rationale: str | None) -> ChangeSet:
            return new_changeset(
                targets,
                rationale,
                id=new_ulid(),
                profile=run.profile,
                run_id=run.id,
                step_id=spec.id,
                source=source,
                created_at=self.clock.now(),
            )

        try:
            changesets = build_changesets(proposal, current=current, snapshots=snapshots, make=make)
            for cs in changesets:
                problems = state.backend.check(cs)
                if problems:
                    detail = "; ".join(
                        f"{p.target.label()} {p.field}: {p.message}" for p in problems
                    )
                    raise StepFailure(f"proposed changes can't be applied: {detail}")
        except ProposalError as exc:
            raise StepFailure(str(exc)) from exc
        except CapabilityError as exc:
            raise StepFailure(f"this profile's backend can't apply changes: {exc}") from exc
        emit = self._change_emitter(state, spec.id)
        for cs in changesets:
            self.changesets.add(cs)
            emit("changeset.proposed", proposed_event(cs))
            if spec.apply == "auto" and not force_review:
                outcome = apply_changeset(
                    cs,
                    backend=state.backend,
                    journal=self.journal,
                    store=self.changesets,
                    new_id=new_ulid,
                    now=self.clock.now,
                    emit=emit,
                )
                self._log_outcome(state, spec.id, outcome.entry)
            else:
                state.holds.setdefault(spec.id, []).append(cs.id)
        return changesets

    def _change_emitter(
        self, state: _RunState, step_id: str
    ) -> Callable[[str, dict[str, JsonValue]], None]:
        return lambda type, data: self._emit(
            state.run, type, step_id, _redact(data, state.redactor)
        )

    def _log_outcome(self, state: _RunState, step_id: str, entry: JournalEntry | None) -> None:
        """Write failures go to the run log too (CHANGESET-10)."""
        for failure in entry.write_failures if entry is not None else []:
            self._emit(
                state.run,
                "run.step.log",
                step_id,
                {
                    "stream": "error",
                    "line": f"{failure.path}: tags not written: {failure.error}",
                    "target": failure.target.id,
                },
            )

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
