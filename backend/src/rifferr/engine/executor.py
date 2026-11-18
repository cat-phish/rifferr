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
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path, PurePath
from typing import Any, Protocol, cast

from pydantic import BaseModel, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.backends.base import RUN_MARKER, CapabilityError, LibraryBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetProposal,
    ChangeSource,
    JournalEntry,
    TargetRef,
)
from rifferr.core.covers import CoverReview
from rifferr.core.files import BusyFolder, CleanupDecision
from rifferr.core.ids import new_ulid
from rifferr.core.imports import ImportDecision, ImportQuestion
from rifferr.core.notify import Notification, SendResult
from rifferr.core.profile import ProfileFile, Redactor, env_var_name, merged_vars
from rifferr.core.review import GateAlbum, GateRequest, ReviewGate
from rifferr.core.selection import Selection
from rifferr.core.snapshot import label
from rifferr.core.workflow import (
    CalledStep,
    Calls,
    Expression,
    InterpolationContext,
    InterpolationError,
    RoundTripDoc,
    StepSpec,
    ValidationError,
    Workflow,
    expand,
    interpolate,
    validate,
)
from rifferr.core.workflow.interpolation import rewrite, substitute
from rifferr.core.workflow.template_fields import interpolate_fields
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
from rifferr.engine.cleanup import CleanupDecisionStore, InMemoryCleanupStore
from rifferr.engine.covers import CoverReviewStore, InMemoryCoverStore
from rifferr.engine.events import Event, EventSink
from rifferr.engine.imports import ImportDecisionStore, InMemoryImportStore
from rifferr.engine.journal import InMemoryJournal, Journal
from rifferr.engine.lanes import (
    Hold,
    InMemoryLaneStore,
    Lane,
    LaneState,
    LaneStore,
    lane_json,
)
from rifferr.engine.review import GateStore, InMemoryGateStore, conflicted_for, fold_changes
from rifferr.engine.run import PassRecord, Run, RunState, Scalar, StepRecord
from rifferr.engine.steps import (
    CleanupRequest,
    CoverRequest,
    GateOptions,
    ImportOptions,
    NoSecrets,
    NotifyTargets,
    SecretAccess,
    StepContext,
    StepFailure,
    StepRefused,
    StepTypeRegistry,
    is_run_level,
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
        notify: NotifyTargets | None = None,
        profile_settings: ProfileFile | None = None,
        busy: Callable[[], list[BusyFolder]] | None = None,
    ) -> None:
        self.run = run
        self.notify = notify
        self.profile_settings = profile_settings
        self.busy = busy
        self.profile_dir = profile_dir
        self.workflow = wf
        self.backend = backend
        self.redactor = redactor
        self.work_dir = work_dir
        self.secrets = secrets
        self.run_supplied: frozenset[str] = frozenset()
        """Names of the variables this run's caller set (SEC-21)."""
        self.cancel = asyncio.Event()
        self.holds: dict[str, list[str]] = {}
        """step id -> ChangeSet ids it proposed that wait for review (CHANGESET-7)."""
        self.gates: dict[str, list[str]] = {}
        """step id -> review gates it opened (S-REVIEW)."""
        self.lanes: dict[str, Lane] = {}
        self.pass_lanes: list[Lane] = []
        self.lane_holds: dict[str, list[Hold]] = {}
        """Album id -> holds raised during the current pass (LANES-5)."""
        self.lane_labels: dict[str, str] = {}
        """Labels for lanes a hold creates (``pending:`` lanes, LANES-14)."""


@dataclass(frozen=True)
class _PassResult:
    stop_run: bool
    failed_albums: set[str]


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
        gates: GateStore | None = None,
        lanes: LaneStore | None = None,
        imports: ImportDecisionStore | None = None,
        cleanups: CleanupDecisionStore | None = None,
        covers: CoverReviewStore | None = None,
    ) -> None:
        self.registry = registry
        self.covers: CoverReviewStore = covers or InMemoryCoverStore()
        self.cleanups: CleanupDecisionStore = cleanups or InMemoryCleanupStore()
        self.imports: ImportDecisionStore = imports or InMemoryImportStore()
        self.lanes: LaneStore = lanes or InMemoryLaneStore()
        self.sink = sink
        self.journal: Journal = journal or InMemoryJournal()
        self.changesets: ChangeSetStore = changesets or InMemoryChangeSetStore()
        self.gates: GateStore = gates or InMemoryGateStore()
        self.clock = clock or SystemClock()
        self._new_id = new_id
        self.cancel_grace = cancel_grace
        self.runs: dict[str, Run] = {}
        self._state: dict[str, _RunState] = {}
        self._listeners: list[Listener] = []

    # -- public API ----------------------------------------------------------

    def resolve_hold(self, run_id: str, ref: str) -> None:
        """Mark a ``ctx.hold`` hold of ``run_id`` resolved (LANES-14)."""
        self.lanes.resolve_hold(run_id, ref)

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
        run_input: str | None = None,
        trigger: str = "manual",
        doc: RoundTripDoc | None = None,
        redactor: Redactor | None = None,
        trusted: bool = False,
        work_dir: Path | None = None,
        secrets: SecretAccess | None = None,
        last_seq: int = 0,
        profile_dir: Path | None = None,
        notify: NotifyTargets | None = None,
        profile_settings: ProfileFile | None = None,
        busy: Callable[[], list[BusyFolder]] | None = None,
        calls: Calls | None = None,
    ) -> Run:
        """Validate and create a queued run (RUN-10, RUN-11).

        ``trusted``: may this run execute shell/python steps (decided by F-SECURITY).
        ``work_dir``: default working directory for steps without a target (the profile dir).
        ``last_seq``: events already stored for this run (a resumed run, RUN-14).
        ``profile_dir``: the profile's folder, for steps that read files from it.
        ``notify``: the profile's notification targets (S-NOTIFY), for ``ctx.notify``.
        ``profile_settings``: ``profile.yaml`` as loaded, for ``ctx.profile_settings``.
        ``busy``: folders other work still needs (FILES-CLEANUP-7), for ``ctx.busy_folders``.
        ``calls``: the workflow files ``wf`` calls, as stored with the job; the run's
        steps are ``wf`` with every call expanded (S-SUBWORKFLOW).
        ``run_input`` / ``trigger``: ``{{ run.input }}`` / ``{{ run.trigger }}``
        (TRIGGERS-5, -6); without an input the workflow's ``input`` is used.
        """
        supplied = {**profile_vars, **(run_vars or {})}
        errors = validate(
            wf,
            self.registry,
            backend.capabilities,
            supplied,
            doc=doc,
            notify_targets=notify.names() if notify is not None else [],
            calls=calls,
        )
        if errors:
            raise RunValidationError(errors)
        if calls is not None:
            wf = expand(wf, calls)
        resolved_vars = merged_vars(supplied, wf.vars)
        if not run_input and wf.input is not None:
            ctx = InterpolationContext(vars=resolved_vars, profile={"name": profile_name})
            try:
                run_input = interpolate(wf.input, ctx)
            except InterpolationError as exc:
                raise RunValidationError(
                    [ValidationError("<workflow>", "input", str(exc))]
                ) from exc
        run = Run(
            id=self._new_id(),
            workflow=wf.name,
            workflow_hash=hashlib.sha256(source).hexdigest(),
            profile=profile_name,
            created_at=self.clock.now(),
            steps=[StepRecord(id=s.id, uses=s.uses) for s in wf.steps],
            vars=resolved_vars,
            trusted=trusted,
            input=run_input or "",
            trigger=trigger,
            last_seq=last_seq,
            called_hashes={
                p: hashlib.sha256(t.encode()).hexdigest()
                for p, t in (calls.texts() if calls is not None else {}).items()
            },
        )
        self.runs[run.id] = run
        run_supplied = frozenset(run_vars or {})
        self._state[run.id] = _RunState(
            run,
            wf,
            backend,
            redactor or Redactor([]),
            work_dir,
            secrets or NoSecrets(),
            profile_dir,
            notify,
            profile_settings,
            busy,
        )
        self._state[run.id].run_supplied = run_supplied
        return run

    def cancel(self, run_id: str) -> None:
        """Request cancellation. No effect on a finished run (RUN-8)."""
        state = self._state[run_id]
        if not state.run.finished:
            state.cancel.set()

    def restore_steps(self, run: Run, records: Mapping[str, StepRecord]) -> None:
        """Put back the records of steps a resumed run already ran (RUN-14, LANES-4)."""
        for i, record in enumerate(run.steps):
            if record.id in records:
                run.steps[i] = records[record.id]

    async def execute(self, run: Run, *, resume_after: str | None = None) -> None:
        """Run the steps as passes over the run's lanes (C-LANES LANES-3).

        A fresh run starts at the first step. A resumed run (RUN-14, LANES-8) continues
        the lanes whose holds were all resolved, from their positions. Returns with the
        run finished, or ``waiting_review`` when only waiting lanes are left (LANES-7).
        """
        state = self._state[run.id]
        specs = state.workflow.steps
        ids = [s.id for s in specs]
        if resume_after is not None and resume_after not in ids:
            raise ValueError(f"run {run.id} has no step {resume_after!r} to resume after")
        resuming = resume_after is not None
        lanes = self.lanes.load(run.id) if resuming else {}
        state.lanes = lanes
        if resume_after is not None:
            if not lanes:  # suspended before C-LANES: every album resumes together
                self._legacy_lanes(state, ids, resume_after)
            else:
                self._release_lanes(state)
        if state.cancel.is_set():
            run.transition(RunState.cancelled)
            self._cancel_lanes(state)
            for record in run.steps:
                if record.status == "pending":
                    record.status = "skipped"
            self._finish(run)
            return
        run.transition(RunState.running)
        run.started_at = self.clock.now()
        if not resuming:
            self._emit(run, "run.started", None, {"workflow": run.workflow, "profile": run.profile})
        else:
            resumed = [lane.key for lane in lanes.values() if lane.state == "active"]
            self._emit(
                run,
                "run.resumed",
                None,
                {"after": resume_after, "lanes": cast(list[JsonValue], list(resumed))},
            )
        start = 0
        if resuming:
            positions = [
                ids.index(lane.position)
                for lane in lanes.values()
                if lane.state == "active" and lane.position is not None
            ]
            start = min(positions, default=len(ids))
        outcome = RunState.succeeded
        for index in range(start, len(specs)):
            spec, record = specs[index], run.steps[index]
            nxt = ids[index + 1] if index + 1 < len(ids) else None
            self._discover(state, spec.id)
            pass_lanes = [
                lane
                for lane in lanes.values()
                if lane.state == "active" and lane.position == spec.id
            ]
            step_type = self.registry.step_type(spec.uses)
            run_level = is_run_level(step_type, spec.with_)
            if not pass_lanes and not (run_level and not lanes):
                # LANES-3: no lane here; run-level steps run lane-less only before any lane
                continue
            result = await self._execute_step(state, spec, record, pass_lanes)
            if record.status == "cancelled":
                outcome = RunState.cancelled
                break
            if result.stop_run:  # LANES-10: a failure with no target under on_error: stop
                outcome = RunState.failed
                for lane in lanes.values():
                    if lane.state == "active":
                        lane.steps[spec.id] = record.status
                        self._end_lane(state, lane, "failed")
                break
            self._advance(state, spec, record, pass_lanes, result.failed_albums, nxt)
        if outcome is RunState.cancelled:
            self._cancel_lanes(state)
        self.lanes.save(run.id, lanes.values())
        if outcome is RunState.succeeded and any(
            lane.state == "waiting" for lane in lanes.values()
        ):
            self._suspend(run, state)
            return
        if outcome is RunState.succeeded and any(lane.state == "failed" for lane in lanes.values()):
            outcome = RunState.failed  # LANES-9
        for record in run.steps:
            if record.status == "pending":
                record.status = "skipped"
        run.transition(outcome)
        self._finish(run)

    # -- lanes (C-LANES) -------------------------------------------------------

    def _legacy_lanes(self, state: _RunState, ids: list[str], resume_after: str) -> None:
        """A run suspended before lanes existed: all its albums resume together."""
        i = ids.index(resume_after)
        position = ids[i + 1] if i + 1 < len(ids) else None
        for album in state.backend.query(Selection.for_run(state.run.id)):
            lane = Lane(album.id, label(album), "active", position)
            if position is None:
                lane.state = "done"
            state.lanes[album.id] = lane
        self.lanes.save(state.run.id, state.lanes.values())

    def _released(self, lane: Lane, resolved: set[str]) -> bool:
        for hold in lane.holds:
            if hold.kind == "changeset":
                stored = self.changesets.get(hold.ref)
                if stored is not None and stored.state in ("proposed", "conflicted"):
                    return False
            elif hold.kind == "gate":
                gate = self.gates.get(hold.ref)
                if gate is not None and gate.state != "continued":
                    return False
            elif hold.ref not in resolved:
                return False
        return True

    def _release_lanes(self, state: _RunState) -> None:
        """LANES-8: waiting lanes whose holds are all resolved become active again."""
        resolved = set(self.lanes.resolved_holds(state.run.id))
        for lane in state.lanes.values():
            if lane.state != "waiting" or not self._released(lane, resolved):
                continue
            lane.holds = []
            lane.state = "active" if lane.position is not None else "done"
            self._emit_lane(state, "lane.resumed", lane)
            if lane.state == "done":
                self._emit_lane(state, "lane.finished", lane)

    def _discover(self, state: _RunState, step_id: str) -> None:
        """LANES-1 new lanes for run albums without one; LANES-12 lanes whose album is gone."""
        run = state.run
        try:
            present = {a.id: a for a in state.backend.query(Selection.for_run(run.id))}
        except CapabilityError:
            return
        for album_id, album in present.items():
            if album_id not in state.lanes:
                lane = Lane(album_id, label(album), "active", step_id)
                state.lanes[album_id] = lane
                self._emit_lane(state, "lane.started", lane)
        for lane in state.lanes.values():
            if lane.state == "active" and not lane.pending and lane.key not in present:
                self._end_lane(state, lane, "gone")

    def _advance(
        self,
        state: _RunState,
        spec: StepSpec,
        record: StepRecord,
        pass_lanes: list[Lane],
        failed: set[str],
        nxt: str | None,
    ) -> None:
        """After a pass: failed lanes end or go on (LANES-10), held lanes wait (LANES-5),
        the others move to the next step."""
        number = len(record.passes)
        held = state.lane_holds
        state.lane_holds = {}
        for key in held:  # holds on albums that had no lane in this pass (e.g. run-level steps)
            if key not in state.lanes:
                state.lanes[key] = Lane(key, state.lane_labels.get(key, key), "active", spec.id)
                self._emit_lane(state, "lane.started", state.lanes[key])
                pass_lanes.append(state.lanes[key])
        for lane in pass_lanes:
            lane.passes[spec.id] = number
            if lane.key in failed:
                lane.steps[spec.id] = "failed"
                if spec.on_error == "stop":
                    self._end_lane(state, lane, "failed")
                    continue
            else:
                lane.steps[spec.id] = record.status
            lane.position = nxt
            holds = held.get(lane.key, [])
            if holds:
                lane.state = "waiting"
                lane.holds = holds
                self._emit_lane(state, "lane.waiting", lane)
            elif nxt is None:
                self._end_lane(state, lane, "done")
        self.lanes.save(state.run.id, state.lanes.values())

    def _end_lane(self, state: _RunState, lane: Lane, final: LaneState) -> None:
        lane.state = final
        lane.position = None
        lane.holds = []
        self._emit_lane(state, "lane.finished", lane)

    def _cancel_lanes(self, state: _RunState) -> None:
        for lane in state.lanes.values():
            if not lane.final:
                self._end_lane(state, lane, "cancelled")
        self.lanes.save(state.run.id, state.lanes.values())

    def _emit_lane(self, state: _RunState, type: str, lane: Lane) -> None:
        self._emit(state.run, type, None, lane_json(lane))

    def _hold(
        self, state: _RunState, key: str, kind: str, ref: str, label: str | None = None
    ) -> None:
        if kind not in ("changeset", "gate", "import", "cleanup", "cover"):
            raise ValueError(f"unknown hold kind {kind!r}")
        state.lane_holds.setdefault(key, []).append(Hold(cast(Any, kind), ref))
        if label is not None:
            state.lane_labels[key] = label

    def _hold_pass(self, state: _RunState, kind: str, ref: str, label: str) -> None:
        """LANES-16: every lane of the pass waits on ``ref``; without lanes, a new one."""
        lanes = [lane for lane in state.pass_lanes if not lane.final]
        if not lanes:
            self._hold(state, f"pending:{kind}:{ref}", kind, ref, label)
            return
        for lane in lanes:
            self._hold(state, lane.key, kind, ref)

    def _request_cleanup(self, state: _RunState, spec: StepSpec, request: CleanupRequest) -> str:
        run = state.run
        now = self.clock.now()
        decision = CleanupDecision(
            id=new_ulid(),
            profile=run.profile,
            run_id=run.id,
            step_id=spec.id,
            path=request.path,
            entries=list(request.entries),
            truncated=request.truncated,
            totals=dict(request.totals),
            options=request.options,
            created_at=now,
            deadline=now + request.timeout if request.timeout is not None else None,
            on_timeout=request.on_timeout,
        )
        self.cleanups.add(decision)
        label = f"cleanup of {PurePath(request.path).name or request.path}"
        self._hold_pass(state, "cleanup", decision.id, label)
        data: dict[str, JsonValue] = {
            "decision_id": decision.id,
            "path": decision.path,
            "totals": cast(dict[str, JsonValue], dict(decision.totals)),
        }
        if decision.deadline is not None:
            data["deadline"] = decision.deadline.isoformat()
        self._emit(run, "cleanup.requested", spec.id, _redact(data, state.redactor))
        return decision.id

    def _request_cover(self, state: _RunState, spec: StepSpec, request: CoverRequest) -> str:
        """COVERS-SEARCH-14: store the review and hold only that album's lane."""
        run = state.run
        review = CoverReview(
            id=new_ulid(),
            profile=run.profile,
            run_id=run.id,
            step_id=spec.id,
            album_id=request.album_id,
            label=request.label,
            query=request.query,
            candidates=list(request.candidates),
            dropped={k: dict(v) for k, v in request.dropped.items()},
            old_sha256=request.old_sha256,
            reason=request.reason,
            created_at=self.clock.now(),
        )
        self.covers.add(review)
        self._hold(state, review.album_id, "cover", review.id)
        data: dict[str, JsonValue] = {
            "review_id": review.id,
            "album_id": review.album_id,
            "label": review.label,
            "candidates": len(review.candidates),
            "reason": review.reason,
        }
        self._emit(run, "cover.requested", spec.id, data)
        return review.id

    def rename_lane(self, run_id: str, old: str, new: str) -> None:
        """LANES-14: a ``pending:`` lane becomes its album's lane once it is imported."""
        self.lanes.rename(run_id, old, new)

    def _suspend(self, run: Run, state: _RunState) -> None:
        waiting = [lane for lane in state.lanes.values() if lane.state == "waiting"]
        holds = [h.ref for lane in waiting for h in lane.holds if h.kind == "changeset"]
        gates = list(
            dict.fromkeys(h.ref for lane in waiting for h in lane.holds if h.kind == "gate")
        )
        last = [r.id for r in run.steps if r.passes]
        run.transition(RunState.waiting_review)
        run.suspended_after = last[-1] if last else run.steps[-1].id
        run.holds = holds
        run.gates = gates
        self._emit(
            run,
            "run.waiting_review",
            None,
            {
                "after": run.suspended_after,
                "changesets": cast(list[JsonValue], list(holds)),
                "gates": cast(list[JsonValue], list(gates)),
                "lanes": cast(list[JsonValue], [lane.key for lane in waiting]),
            },
        )

    # -- steps ---------------------------------------------------------------

    async def _execute_step(
        self, state: _RunState, spec: StepSpec, record: StepRecord, pass_lanes: list[Lane]
    ) -> _PassResult:
        """Run one pass of a step over ``pass_lanes`` (LANES-3)."""
        run = state.run
        number = len(record.passes) + 1
        state.pass_lanes = pass_lanes
        record.status = "running"
        record.error = None
        record.started_at = self.clock.now()
        started: dict[str, JsonValue] = {
            "uses": spec.uses,
            "pass": number,
            "lanes": len(pass_lanes),
        }
        if isinstance(spec, CalledStep):  # SUBWORKFLOW-6
            started["call_path"] = list(spec.call_path)
            started["workflow"] = spec.workflow
        self._emit(run, "run.step.started", spec.id, started)
        ctx: StepContext | None = None
        refused = False
        try:
            selection = self._selection(state, spec, pass_lanes)
            params = self._prepare_params(state, spec)
            step_type = self.registry.step_type(spec.uses)
            assert step_type is not None, "validated before the run was created"
            ctx = StepContext(
                run_id=run.id,
                step=spec,
                params=params,
                backend=state.backend,
                selection=selection,
                emit=lambda type, data: self._emit_from_step(state, spec.id, type, data),
                is_cancelled=state.cancel.is_set,
                profile=run.profile,
                vars=run.vars,
                run_input=run.input,
                work_dir=state.work_dir,
                trusted=run.trusted,
                secrets=state.secrets,
                on_secret=state.redactor.add,
                redact=state.redactor.redact,
                propose=lambda proposal, current, snapshots: self._propose(
                    state, spec, proposal, current, snapshots
                ),
                profile_dir=state.profile_dir,
                interpolate=lambda value: self._interpolate_text(state, value),
                interpolate_template=lambda value: self._interpolate_template_text(state, value),
                open_gate=lambda requests, options, snapshots: self._open_gate(
                    state, spec, requests, options, snapshots
                ),
                on_hold=lambda key, kind, ref, label: self._hold(state, key, kind, ref, label),
                request_import=lambda question, options: self._request_import(
                    state, spec, question, options
                ),
                notify=(
                    (lambda targets, n: self._notify(state, spec.id, targets, n))
                    if state.notify is not None
                    else None
                ),
                notify_targets=state.notify.names() if state.notify is not None else (),
                base_url=state.notify.base_url if state.notify is not None else None,
                profile_settings=state.profile_settings,
                busy_folders=state.busy,
                request_cleanup=lambda request: self._request_cleanup(state, spec, request),
                request_cover=lambda request: self._request_cover(state, spec, request),
                hold_pass=lambda kind, ref, label: self._hold_pass(state, kind, ref, label),
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
        failed_albums: set[str] = set()
        if ctx is not None:
            record.failed_targets = list(ctx.failed_targets)
            for failure in ctx.failed_targets:
                album = ctx._snapshots.album_for(failure.target)  # pyright: ignore[reportPrivateUsage]
                failed_albums.add(album or failure.target)
        else:
            record.failed_targets = []
        if record.error is not None and record.status == "failed":
            record.outputs = {}
        record.finished_at = self.clock.now()
        record.passes.append(
            PassRecord(
                number=number,
                lanes=len(pass_lanes),
                status=record.status,
                outputs=dict(record.outputs),
                error=record.error,
                started_at=record.started_at,
                finished_at=record.finished_at,
            )
        )
        data: dict[str, JsonValue] = {
            "status": record.status,
            "outputs": cast(dict[str, JsonValue], dict(record.outputs)),
            "failed_targets": [
                {"target": f.target, "message": f.message} for f in record.failed_targets
            ],
            "pass": number,
            "lanes": len(pass_lanes),
        }
        if record.error is not None:
            data["error"] = state.redactor.redact(record.error)
        if refused:
            data["refused"] = True  # e.g. needs trust; lets clients give a hint (CLI-10)
        self._emit(run, "run.step.finished", spec.id, _redact(data, state.redactor))
        # A failure with no target ends the pass; under stop it ends the run (LANES-10).
        no_target = record.status == "failed" and record.error is not None
        stop_run = refused or (no_target and spec.on_error == "stop")
        return _PassResult(stop_run=stop_run, failed_albums=failed_albums)

    def _selection(self, state: _RunState, spec: StepSpec, pass_lanes: list[Lane]) -> Selection:
        """The step's albums: this pass's lanes, narrowed by the ``when`` of every call
        around a called step (SUBWORKFLOW-7)."""
        run = state.run
        album_ids: tuple[str, ...] | None = None
        if state.lanes:
            album_ids = tuple(lane.key for lane in pass_lanes if not lane.pending)
        narrow = spec.narrow if isinstance(spec, CalledStep) else ()
        for when in narrow:
            query = self._interpolate_text(state, when)
            matching = state.backend.query(
                Selection(query=query, run_id=run.id, album_ids=album_ids)
            )
            album_ids = tuple(a.id for a in matching)
        return Selection(query=spec.when or "", run_id=run.id, album_ids=album_ids)

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
    def _interpolation_context(state: _RunState) -> InterpolationContext:
        """``{{ steps.<id>.<key> }}`` comes from the latest pass of that step that
        included this pass's lanes (LANES-4)."""
        run = state.run
        outputs: dict[str, Mapping[str, Scalar]] = {}
        for r in run.steps:
            if r.status not in ("succeeded", "failed") and not r.passes:
                continue
            numbers = [lane.passes[r.id] for lane in state.pass_lanes if r.id in lane.passes]
            outputs[r.id] = r.outputs_of(max(numbers) if numbers else None)
        return InterpolationContext(
            vars=run.vars,
            run={
                "id": run.id,
                "workflow": run.workflow,
                "input": run.input,
                "trigger": run.trigger,
            },
            profile={"name": run.profile},
            steps=outputs,
        )

    def _interpolate_text(self, state: _RunState, value: str) -> str:
        try:
            return interpolate(value, self._interpolation_context(state))
        except InterpolationError as exc:
            raise StepFailure(str(exc)) from exc

    def _interpolate_template(self, state: _RunState, ctx: InterpolationContext, value: str) -> str:
        """``{{ }}`` for text the backend then evaluates as a template (SEC-22): values
        from outside the workflow (run fields, run-supplied vars, step outputs) are
        escaped so the backend reads them as literal text. Workflow and profile values
        stay templates."""
        escape = state.backend.escape_template

        def resolve(path: tuple[str, ...]) -> str:
            text = ctx.resolve(path)
            root, *rest = path
            outside = root in ("run", "steps") or (
                root == "vars" and len(rest) == 1 and rest[0] in state.run_supplied
            )
            return escape(text) if outside else text

        return substitute(value, resolve)

    def _interpolate_template_text(self, state: _RunState, value: str) -> str:
        try:
            return self._interpolate_template(state, self._interpolation_context(state), value)
        except InterpolationError as exc:
            raise StepFailure(str(exc)) from exc

    def _prepare_params(self, state: _RunState, spec: StepSpec) -> BaseModel:
        ctx = self._interpolation_context(state)
        step_type = self.registry.step_type(spec.uses)
        assert step_type is not None
        try:
            _refuse_run_data_in_code(step_type.params_model, spec.with_, state.run_supplied)
            plain = partial(interpolate, ctx=ctx)
            # SEC-22: a run-level step evaluates no templates, so its values stay as is.
            template = (
                plain
                if is_run_level(step_type, spec.with_)
                else partial(self._interpolate_template, state, ctx)
            )
            values = interpolate_fields(step_type.params_model, spec.with_, plain, template)
        except InterpolationError as exc:
            raise StepFailure(str(exc)) from exc
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
            if apply_policy(spec, step_type) == "auto" and not force_review:
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
                album = snapshots.album_of(cs.targets[0].target)
                state.lane_holds.setdefault(album, []).append(Hold("changeset", cs.id))  # LANES-5
        return changesets

    # -- review gates (S-REVIEW) ---------------------------------------------------

    def _open_gate(
        self,
        state: _RunState,
        spec: StepSpec,
        requests: list[GateRequest],
        options: GateOptions,
        snapshots: SnapshotIndex,
    ) -> str:
        run = state.run
        entries = self.journal.for_run(run.id)
        changesets = self.changesets.for_run(run.id, "conflicted")
        albums: list[GateAlbum] = []
        for request in requests:
            album = snapshots.album(request.album_id)
            if album is None:
                raise StepFailure(f"album {request.album_id} is not one of this step's targets")
            albums.append(
                GateAlbum(
                    target=TargetRef(scope="album", id=album.id),
                    label=label(album),
                    message=request.message,
                    shown=dict(request.shown),
                    changes=fold_changes(entries, album),
                    conflicted=conflicted_for(changesets, album),
                    imported_by_run=album.fields.get(RUN_MARKER) == run.id,
                )
            )
        now = self.clock.now()
        gate = ReviewGate(
            id=new_ulid(),
            profile=run.profile,
            run_id=run.id,
            step_id=spec.id,
            albums=albums,
            on_reject=options.on_reject,
            on_timeout=options.on_timeout,
            created_at=now,
            deadline=now + options.timeout if options.timeout is not None else None,
        )
        self.gates.add(gate)
        state.gates.setdefault(spec.id, []).append(gate.id)
        for album in albums:  # LANES-5: each album in the gate waits
            state.lane_holds.setdefault(album.target.id, []).append(Hold("gate", gate.id))
        data: dict[str, JsonValue] = {"gate_id": gate.id, "albums": len(albums)}
        if gate.deadline is not None:
            data["deadline"] = gate.deadline.isoformat()
        self._emit(run, "review.requested", spec.id, data)
        return gate.id

    # -- import decisions (F-IMPORT-UI) -------------------------------------------

    def _request_import(
        self, state: _RunState, spec: StepSpec, question: ImportQuestion, options: ImportOptions
    ) -> str:
        run = state.run
        now = self.clock.now()
        decision = ImportDecision(
            id=new_ulid(),
            profile=run.profile,
            run_id=run.id,
            step_id=spec.id,
            kind=question.kind,
            path=question.path,
            fingerprint=question.fingerprint,
            items=list(question.items),
            candidates=list(question.candidates),
            duplicates=list(question.duplicates),
            options=dict(options.options),
            set_fields=dict(options.set_fields),
            created_at=now,
            deadline=now + options.timeout if options.timeout is not None else None,
            on_timeout=options.on_timeout,
        )
        self.imports.add(decision)
        label = PurePath(question.path).name or question.path
        self._hold(state, decision.lane_key, "import", decision.id, label)  # IMPORT-UI-5
        data: dict[str, JsonValue] = {
            "decision_id": decision.id,
            "path": decision.path,
            "kind": decision.kind,
            "candidates": len(decision.candidates),
        }
        if decision.deadline is not None:
            data["deadline"] = decision.deadline.isoformat()
        self._emit(run, "import.requested", spec.id, _redact(data, state.redactor))
        return decision.id

    async def _notify(
        self, state: _RunState, step_id: str, targets: list[str] | None, n: Notification
    ) -> list[SendResult]:
        """``ctx.notify`` (S-NOTIFY NOTIFY-4, -12): send to each target at once and emit
        the outcome of each."""
        assert state.notify is not None
        names = state.notify.names() if targets is None else list(dict.fromkeys(targets))
        unknown = [t for t in names if t not in state.notify.names()]
        if unknown:
            raise StepFailure(f"unknown notification target(s): {', '.join(unknown)}")
        results = await asyncio.gather(*(state.notify.send(t, n) for t in names))
        for result in results:
            data: dict[str, JsonValue] = {"target": result.target, "source": "step"}
            if result.ok:
                self._emit(state.run, "notify.sent", step_id, data)
            else:
                data["error"] = state.redactor.redact(result.error or "")
                self._emit(state.run, "notify.failed", step_id, data)
        return list(results)

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


def _refuse_run_data_in_code(
    model: type[BaseModel], with_: Mapping[str, Any], run_supplied: frozenset[str]
) -> None:
    """SEC-21: a string in a field marked ``code_text`` (a shell command) is code, so it
    may not contain data that comes from outside the workflow: the run input, variables
    the run's caller set, or step outputs. Those reach the process through its
    environment (``RIFFERR_RUN_INPUT``, ``RIFFERR_VAR_<NAME>``)."""
    for name, field in model.model_fields.items():
        extra = cast(Any, field.json_schema_extra)
        value: Any = with_.get(name)
        if not isinstance(extra, dict) or not extra.get("code_text"):  # type: ignore[reportUnknownMemberType]
            continue
        if not isinstance(value, str):
            continue

        def check(expr: Expression, field_name: str = name) -> str:
            root, *rest = expr.path
            if root == "run" and rest == ["input"]:
                raise StepFailure(
                    f"{expr.text} can't be written into '{field_name}': it would be run as code. "
                    "Use $RIFFERR_RUN_INPUT"
                )
            if root == "vars" and len(rest) == 1 and rest[0] in run_supplied:
                raise StepFailure(
                    f"{expr.text} was set for this run and can't be written into "
                    f"'{field_name}': it would be run as code. "
                    f"Use ${env_var_name(rest[0])}"
                )
            if root == "steps":
                raise StepFailure(
                    f"{expr.text} can't be written into '{field_name}': step outputs may hold "
                    "data from outside the workflow and would be run as code"
                )
            return expr.text

        rewrite(value, check)


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


def apply_policy(spec: StepSpec, step_type: object) -> str:
    """The step's ``apply``, or its type's ``default_apply`` when the workflow doesn't
    say (CHANGESET-6; ``covers.search`` defaults to ``auto``, COVERS-SEARCH-15)."""
    if "apply" in spec.model_fields_set:
        return spec.apply
    return str(getattr(step_type, "default_apply", spec.apply))
