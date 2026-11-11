# C-RUN: Runs, step execution, events and cancellation
Status: Draft
Milestone: M0

## Context
A run is one execution of a workflow against a profile, with a state machine,
an event log and (from M1) a review queue and journal (architecture §2). Every
long operation emits structured events and is cancellable; the UI subscribes
over SSE and never polls (AGENTS.md conventions). This spec covers the M0
engine in `rifferr.engine`: sequential steps over a whole run. Lanes (one per
album) arrive in M2.

## Behavior
- RUN-1: A run MUST move through states `queued → running → (succeeded | failed | cancelled)`; any other transition MUST raise.
- RUN-2: Steps MUST execute in file order. Before each step, the engine MUST interpolate its `with:` values (C-WORKFLOW) and evaluate its selection; a step with a `when:` condition acts only on targets for which `backend.matches` is true.
- RUN-3: Step types MUST be discovered through the `rifferr.steps` entry-point group; built-in steps MUST be registered the same way (AGENTS.md non-negotiable 7).
- RUN-4: A step type MUST provide a parameter model, the backend capabilities it requires, and an async `run(ctx)` that reports progress only through `ctx.emit(...)`.
- RUN-5: When a step fails, `on_error` MUST decide: `stop` fails the run and skips the rest; `skip` marks the step failed and continues with the next step; `continue` marks the failing targets and continues the same step for other targets, then continues the run.
- RUN-6: The engine MUST emit events `run.started`, `run.step.started`, `run.step.log`, `run.step.finished`, `run.finished`, each with a monotonically increasing per-run `seq`, a UTC timestamp, `run_id` and (where relevant) `step_id`.
- RUN-7: Events MUST be persisted through an injected `EventSink` before being published, so a subscriber that reconnects with its last `seq` receives every later event exactly once.
- RUN-8: Cancelling a run MUST stop the current step cooperatively within 5 seconds (steps check `ctx.cancelled`; subprocess steps are terminated), mark the run `cancelled` and emit `run.finished` with that status.
- RUN-9: The engine MUST be pure apart from injected interfaces (backend, event sink, clock, step registry); it MUST be runnable in tests with `FakeBackend` and an in-memory sink.
- RUN-10: Validation (C-WORKFLOW `validate`) MUST pass before a run is created; a workflow that fails validation never reaches `running`.
- RUN-11: Each run MUST record the workflow content hash and profile name it ran with.

## Interfaces
```python
class RunState(StrEnum): queued, running, succeeded, failed, cancelled

class Event(BaseModel):
    seq: int
    ts: datetime
    run_id: str
    type: str                    # "run.step.log", ...
    step_id: str | None = None
    data: dict[str, JsonValue]

class StepContext(Protocol):
    run_id: str
    step: StepSpec
    params: BaseModel            # validated, interpolated `with:`
    backend: LibraryBackend
    selection: Selection
    cancelled: bool
    def emit(self, type: str, **data: JsonValue) -> None: ...

class StepType(Protocol):
    name: str                    # "shell"
    params_model: type[BaseModel]
    requires: BackendCapabilities
    async def run(self, ctx: StepContext) -> StepResult: ...

class EventSink(Protocol):
    def append(self, event: Event) -> None: ...
    def since(self, run_id: str, seq: int) -> Iterable[Event]: ...
```

## Examples
Workflow with steps `a` (on_error: skip, fails) and `b` (succeeds) → events:
`run.started`, `run.step.started{a}`, `run.step.finished{a, status: failed}`,
`run.step.started{b}`, `run.step.finished{b, status: succeeded}`,
`run.finished{status: succeeded}`.

## Extensibility
Third-party step types register under `rifferr.steps` and get the same `StepContext` as built-ins. Event type names and the envelope are fixed and public; `data` is per event type.

## Security & safety
- `run.step.log` data passes through the profile's secret redaction (PROFILE-5) before persisting.
- Step types cannot reach the app DB or other profiles; they only get `StepContext`.

## Non-goals
- Lanes, review pauses, resume after days (M1–M2).
- Concurrency limits and trigger queues (F-TRIGGERS, M2).
- ChangeSet application (C-CHANGESET, M1).

## Open questions
- Does `on_error: continue` make sense for steps with no per-target granularity (e.g. a single `per: run` shell command), or does it behave like `skip` there?
- Should step outputs (`steps.<id>.<key>`) be part of `StepResult` in M0?
- Run IDs: ULID, UUIDv7, or something shorter for display? (Used in `rifferr_run` flexible attribute values and URLs.)

## Tasks
- [ ] run state machine
- [ ] step registry via entry points
- [ ] executor with on_error handling
- [ ] event model + in-memory sink + replay-from-seq
- [ ] cancellation
