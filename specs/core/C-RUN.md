# C-RUN: Runs, step execution, events and cancellation
Status: Implemented
Milestone: M0

## Context
A run is one execution of a workflow against a profile, with a state machine,
an event log and (from M1) a review queue and journal (architecture §2). Every
long operation emits structured events and is cancellable; the UI subscribes
over SSE and never polls (AGENTS.md conventions). This spec covers the M0
engine in `rifferr.engine`: sequential steps over a whole run. Lanes (one per
album) arrive in M2.


M1 addition RUN-14 (run suspension on holds, state `waiting_review`) is specified in [S-REVIEW](../steps/S-REVIEW.md) (approved 2026-11-11), generalizing C-CHANGESET CHANGESET-7.
## Behavior
- RUN-1: A run MUST move through states `queued → running → (succeeded | failed | cancelled)`; any other transition MUST raise.
- RUN-2: Steps MUST execute in file order. Before each step, the engine MUST interpolate its `with:` values (C-WORKFLOW) and evaluate its selection; a step with a `when:` condition acts only on targets for which `backend.matches` is true.
- RUN-3: Step types MUST be discovered through the `rifferr.steps` entry-point group; built-in steps MUST be registered the same way (AGENTS.md non-negotiable 7).
- RUN-4: A step type MUST provide a parameter model, the backend capabilities it requires, and an async `run(ctx)` that reports progress only through `ctx.emit(...)`.
- RUN-5: When a step fails, `on_error` MUST decide: `stop` fails the run and skips the rest; `skip` marks the step failed and continues with the next step; `continue` marks the failing targets and continues the same step for other targets, then continues the run. Steps report a per-target failure with `ctx.fail_target(...)`, which returns under `continue` and ends the step otherwise. A step failure with no target (for example a `per: run` command, or an unexpected exception) ends the step, so `continue` behaves like `skip` there. A run whose steps failed only under `skip`/`continue` still succeeds; the step records show the failures.
- RUN-6: The engine MUST emit events `run.started`, `run.step.started`, `run.step.log`, `run.step.finished`, `run.finished`, each with a monotonically increasing per-run `seq`, a UTC timestamp, `run_id` and (where relevant) `step_id`.
- RUN-7: Events MUST be persisted through an injected `EventSink` before being published, so a subscriber that reconnects with its last `seq` receives every later event exactly once.
- RUN-8: Cancelling a run MUST stop the current step cooperatively within 5 seconds (steps check `ctx.cancelled`; subprocess steps are terminated), mark the run `cancelled` and emit `run.finished` with that status.
- RUN-9: The engine MUST be pure apart from injected interfaces (backend, event sink, clock, step registry); it MUST be runnable in tests with `FakeBackend` and an in-memory sink.
- RUN-10: Validation (C-WORKFLOW `validate`) MUST pass before a run is created; a workflow that fails validation never reaches `running`.
- RUN-11: Each run MUST record the workflow content hash and profile name it ran with.
- RUN-12: A step MAY return outputs (a map of string, number or boolean values); later steps read them as `{{ steps.<id>.<key> }}`. Referencing an output the step didn't return fails the referencing step.
- RUN-13: Run IDs MUST be ULIDs (26-character Crockford base32, time-sortable), generated without third-party dependencies.

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

As implemented (`rifferr.engine`):
- `Engine(registry, sink, clock=, new_id=, cancel_grace=5.0)`;
  `create_run(wf, source_bytes, profile_name=, profile_vars=, backend=, run_vars=, doc=, redactor=)`
  validates and returns a queued `Run`; `await execute(run)`; `cancel(run_id)`;
  `subscribe(listener)` gets each event after the sink has stored it.
- Vars precedence: workflow defaults < profile vars < run vars (CLI `--var` / API).
- `StepContext`: `params`, `backend`, `selection` (the run's albums narrowed by `when`),
  `albums(scope)`, `log(line, stream)`, `emit("run.step.<name>", ...)`, `fail_target(target,
  message)`, `cancelled`. Steps can't emit the engine's own event types.
- Step `with:` values are interpolated, then validated against the step's parameter model, when
  the step starts; a failure there fails the step.
- Event `data`: `run.started {workflow, profile}`, `run.step.started {uses}`,
  `run.step.log {stream, line}`, `run.step.finished {status, outputs, failed_targets, error?, refused?}` (`refused: true` when the step raised `StepRefused`, added for F-CLI),
  `run.finished {status}`. String values from steps pass through the profile's redactor.
- Step statuses: pending, running, succeeded, failed, skipped, cancelled.
- Entry-point loading: `rifferr.steps.registry.load_step_types()`; broken plugins are listed in
  `registry.errors` instead of stopping the app.
- ULIDs: `rifferr.core.ids.new_ulid()`.
- Added for S-SHELL: `create_run(..., trusted=False, work_dir=None, secrets=None)`. `trusted`
  is stored on the `Run` (F-SECURITY will decide it); `work_dir` is the default directory for
  steps without a target (the profile dir); `secrets` is a `SecretAccess`
  (`rifferr.credentials.ProfileSecrets` for a real profile). `StepContext` also has `profile`,
  `vars`, `work_dir`, `trusted`, `secret(name)` (resolves on demand and adds the value to the
  run's redactor) and `secret_env_refs()`. `log(line, stream, target=None)` adds `target` to
  `run.step.log` data. Raising `StepRefused` fails the step and stops the run whatever
  `on_error` says.

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
- ~~`on_error: continue` without per-target granularity?~~ Resolved 2026-11-11 (author): behaves like `skip` (RUN-5).
- ~~Step outputs in M0?~~ Resolved 2026-11-11 (author): yes (RUN-12).
- ~~Run ID format?~~ Resolved 2026-11-11 (author): ULID, stdlib-only (RUN-13).

## Tasks
- [x] run state machine
- [x] step registry via entry points
- [x] executor with on_error handling
- [x] event model + in-memory sink + replay-from-seq
- [x] cancellation
- [x] step outputs + ULID run ids
