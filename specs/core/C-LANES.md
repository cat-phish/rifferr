# C-LANES: Lanes, per-album progress through a run
Status: Draft
Milestone: M2

## Context
Architecture §2a: "an album waiting at a review gate doesn't hold up the others." Up to M1
a run is one sequence of steps over all of its albums. Any hold, whether a pending
`apply: review` ChangeSet (C-CHANGESET CHANGESET-7) or a review gate (S-REVIEW REVIEW-3),
suspends the whole run (RUN-14). With unattended imports (F-TRIGGERS), a folder with ten
albums where one needs a look would leave nine albums half-processed until someone
answers.

A **lane** is one album's path through a run. This spec splits a run into lanes so holds
pause only the albums they are about. It amends C-RUN (RUN-5, RUN-14), C-CHANGESET
(CHANGESET-7) and S-REVIEW (REVIEW-3, -7, -8). It is its own spec, like S-REVIEW was for
RUN-14, so C-RUN stays Implemented; C-RUN links here.

How steps see albums today (unchanged by this spec): a step's selection is the run's
albums, meaning those carrying the run marker `rifferr_run` (`Selection.for_run`), narrowed
by `when:`. Steps get many albums per call and propose one ChangeSet per album
(CHANGESET-3).

Out of scope: import decisions (F-IMPORT-UI, next) put albums that aren't imported yet on
hold. This spec gives them the hook they need (LANES-14) but not the decisions.

## Behavior

### Lanes and positions
- LANES-1: Before each step starts, the engine MUST create a lane for every album in the run's selection (`Selection.for_run`, ignoring `when:`) that has no lane yet. A new lane's position is that step. Lanes are keyed by the backend album id and persisted with the run (`run_lanes`).
- LANES-2: Each lane MUST have a state: `active` (will run its next step), `waiting` (has holds), `done` (ran the last step), `failed`, `rejected` (rejected at a gate), `gone` (the album no longer exists in the library) or `cancelled`. Every lane state change MUST emit an event: `lane.started`, `lane.waiting` (with its holds), `lane.resumed`, `lane.finished` (with its final state). These go on the run's stream and on `/api/events`.
- LANES-3: A run MUST execute steps in file order as **passes**. Each pass of a step runs once, with `ctx.selection` restricted (`Selection.ids`) to the active lanes positioned at that step, and still narrowed by `when:`. Lanes whose position is later skip it. After the pass, those lanes move to the next step unless the pass put them on hold (LANES-5) or ended them (LANES-8). A pass with no lanes doesn't run, and emits nothing, unless the step is a run-level step (LANES-6).
- LANES-4: The step record MUST keep one entry per pass: lane count, status, outputs and timings. `run.step.started` and `run.step.finished` MUST carry `pass` (1, 2, …) and `lanes` (count). `{{ steps.<id>.<key> }}` MUST resolve to the outputs of the most recent pass of that step that included the lane being processed. When a pass covers several lanes, they all ran in that same pass, so the result is unambiguous.

### Holds
- LANES-5: A hold MUST pause only the lanes it is about:
  - after a step with `apply: review`, each album with a pending ChangeSet from that pass;
  - a review gate, each album in the gate;
  - a hold a step raises with `ctx.hold(album_id, kind, ref)` (LANES-14).

  Held lanes become `waiting` after that step. Lanes without holds continue in the same run without waiting. This replaces "the run MUST be suspended" in CHANGESET-7 and REVIEW-3.
- LANES-6: A step type MAY declare `run_level = True`. A run-level step runs once per pass with every lane that has reached it, and also with zero lanes. Examples are `beets.import` (it brings the albums in, so it must run before any lane exists), `shell` with `per: run`, and later `notify` (S-NOTIFY) and `files.cleanup` (S-FILES-CLEANUP). Its `ctx.selection` is still the pass's lanes, so `per: run` scripts can tell which albums arrived (open question 2).
- LANES-7: The run's state MUST follow its lanes. It is `running` while any lane is active. When no lane is active, it is `waiting_review` if any lane is waiting. Otherwise it reaches its final state (LANES-9). While `waiting_review` the run holds no worker (RUN-14 unchanged).
- LANES-8: When all of a waiting lane's holds are resolved, the server MUST queue a resume job for the run, unless one is already queued, in which case the lane joins it. The resume job runs passes from the earliest position among the resumed lanes (LANES-3). Lanes that resume at different times run in different passes. A lane whose hold was resolved by a rejection ends there (LANES-11).

### Failure, rejection, gone albums
- LANES-9: A run's final state MUST be:
  - `cancelled` if it was cancelled;
  - `failed` if a run-level failure ended it (a failure with no target, or a `run_level` step's failure under `on_error: stop`) or if any lane ended `failed` (open question 3);
  - otherwise `succeeded`.

  `RunSummary` MUST carry lane counts by state.
- LANES-10: RUN-5 per lane: when a step fails for a target (`ctx.fail_target`):
  - `on_error: stop` ends that album's lane `failed`, and other lanes continue;
  - `skip` and `continue` mark the step failed for that lane, and the lane continues with the next step.

  A failure with no target ends the pass. Under `stop` it ends the run and every active lane is `failed`; under `skip`/`continue` the lanes of that pass move on.
- LANES-11: Rejecting a review gate MUST end only the gate's albums' lanes `rejected`. Its `proposed` ChangeSets for those albums become `rejected`, and with `on_reject: undo` only those albums' applied ChangeSets of this run are undone (newest first, JOURNAL-6 per album). Other lanes are unaffected (open question 4). Rejecting an `apply: review` ChangeSet resolves that hold and the lane continues (CHANGESET-9 unchanged).
- LANES-12: If a lane's album no longer exists when its next pass starts (for example a `beets.command` removed it), the lane MUST end `gone`. That is not a failure.
- LANES-13: Cancelling a run MUST end every non-final lane `cancelled`, apply RUN-8 to the current pass, and reject pending ChangeSets and cancel pending gates as today (CHANGESET-7, REVIEW-10).

### Hooks for other specs
- LANES-14: `StepContext.hold(key, kind, ref)` MUST put the lane `key` on hold until `ref` is resolved through `Engine.resolve_hold(run_id, ref)`. `key` is a backend album id, or `pending:<id>` for an album the step hasn't imported yet (F-IMPORT-UI). A `pending:` lane MUST be renamed to its album id once the album exists, keeping its position and history.
- LANES-15: `GET /api/runs/{id}` MUST include the run's lanes: album id, label, state, position (step id), holds, and the steps it ran with their status. The run page MUST show a lane table: album, state, current or last step, and a link to its holds in the review queue.

## Interfaces
```python
LaneState = Literal["active", "waiting", "done", "failed", "rejected", "gone", "cancelled"]

@dataclass
class Lane:
    key: str                       # album id, or "pending:<id>" (LANES-14)
    label: str                     # "Artist - Album" at creation
    state: LaneState
    position: str | None           # id of the next step; None when final
    holds: list[Hold]
    steps: dict[str, str]          # step id -> status for this lane

@dataclass(frozen=True)
class Hold:
    kind: Literal["changeset", "gate", "import"]
    ref: str                       # ChangeSet id, gate id, import decision id

class StepType(Protocol):
    run_level: ClassVar[bool] = False    # LANES-6

class StepContext:
    def hold(self, key: str, kind: str, ref: str) -> None: ...   # LANES-14
```
App DB (new migration):
```sql
CREATE TABLE run_lanes (
  run_id TEXT, key TEXT, label TEXT, state TEXT, position TEXT,
  holds TEXT,        -- JSON list of {kind, ref}
  steps TEXT,        -- JSON {step_id: status}
  updated_at TEXT,
  PRIMARY KEY (run_id, key)
);
```
Events: `lane.started {key, label, position}`, `lane.waiting {key, holds}`, `lane.resumed {key, position}`, `lane.finished {key, state}`. `run.step.started/finished` gain `pass` and `lanes`.

API: `RunDetail.lanes: list[LaneOut]`, `RunSummary.lanes: {state: count}`.

## Examples
Workflow: `import` → `release-types` (`apply: auto`) → `compilations` (`apply: review`) → `notify` (run-level). The inbox folder holds albums A, B and C. C is a compilation.

1. `import` (run-level, pass 1, no lanes yet) imports A, B and C → lanes A, B, C at `release-types`.
2. `release-types` pass 1 with A, B, C → all to `compilations`.
3. `compilations` pass 1 with A, B, C proposes a ChangeSet for C only → C `waiting`; A, B to `notify`.
4. `notify` pass 1 with A, B → A, B `done`. No active lanes, C waiting → run `waiting_review`, worker released.
5. Two days later C's ChangeSet is accepted → resume job → `notify` pass 2 with C → C `done` → run `succeeded`.

Gate rejection: a gate over A and B is rejected with `on_reject: undo` → A and B's applied changes from this run are undone, A and B `rejected`; C continues.

## Extensibility
- Step types choose `run_level`; everything else is per lane by default.
- Any step can raise holds with `ctx.hold` (import decisions, cover choices in M3, AI
  review in M4) without engine changes.
- Fixed: lanes are per album. There are no item-level lanes.

## Security & safety
- A resumed lane runs the workflow text stored with the run's job (SECURITY-10 unchanged),
  never a newer file.
- Lane-scoped undo (LANES-11) only touches journal entries of this run for those albums.

## Non-goals
- Running lanes in parallel within a pass (open question 6).
- Per-album decisions on a multi-album gate (F-REVIEW-QUEUE update with the queue dashboard).
- Import decisions themselves (F-IMPORT-UI).
- Item-level lanes.

## Open questions
1. **Execution model.** I recommend passes: a step runs once for all lanes that are at it,
   so `beets.command` stays one invocation per batch and steps keep their batch API. The
   alternative is calling every step once per lane, which is simpler to reason about, but
   N times slower for subprocess steps, and it would change every step type. Passes?
2. **Run-level steps after a split.** I recommend they run once per pass, so a run-level
   step can run more than once in a run: a `notify` after a review step sends once for the
   albums done now and again for the ones resumed later. The alternative is a barrier:
   run-level steps wait until every lane has reached them, so one album waiting for days
   delays the rest's notification. Per pass, or barrier? Or a per-step choice
   (`wait_for_all: true`)?
3. **Final state with failed lanes.** I recommend `failed` if any lane failed (LANES-9),
   so an unattended run with one broken album is visibly failed and S-NOTIFY's "run
   failed" fires. The alternative is `succeeded` with a failed-lane count, matching how
   RUN-5 treats `skip`/`continue`. Which?
4. **Gate rejection scope.** I recommend that rejecting a gate ends and undoes only its
   albums' lanes (LANES-11). Today it ends the whole run. OK?
5. **Resume coalescing.** I recommend one queued resume job per run at a time; lanes
   resolved meanwhile join it (LANES-8). OK?
6. **Parallelism.** Architecture says "lanes run in parallel up to a limit." I recommend
   deferring it to the BACKLOG. Passes already stop one album from blocking others, and
   beets' library is one SQLite file written from one worker, so parallel writers would
   need more care. A later `concurrency:` on `per: album` steps (shell) is the likely
   shape. Defer?
7. **Albums that join late.** LANES-1 creates a lane for any run album that appears before
   a step, for example a second `beets.import`, positioned at that step. Such a lane skips
   steps before it. Alternative: refuse workflows with more than one album-producing
   step. Keep LANES-1?
8. **Pending lanes for F-IMPORT-UI.** Is `pending:<id>`, renamed once imported (LANES-14),
   the right hook, or should import decisions stay outside lanes until imported?

## Tasks
- [ ] Engine: lanes, passes, positions; `run_level`; per-pass step records and outputs
      (LANES-1, -3, -4, -6).
- [ ] Holds per lane for `apply: review`, gates and `ctx.hold`; resume jobs with coalescing
      (LANES-5, -7, -8, -14); update CHANGESET-7, REVIEW-3/-7/-8 wording.
- [ ] Failure, rejection, gone and cancel per lane; lane-scoped undo (LANES-9 to -13).
- [ ] Migration `run_lanes`; lane events; `/api/events` carries them.
- [ ] API `RunDetail.lanes`, `RunSummary.lanes`; regenerate schemas and TS client.
- [ ] Run page lane table; review queue links per lane (LANES-15); Playwright: one album
      waits, the others finish.
- [ ] `beets.import` and `shell` `per: run` are `run_level`; S-BEETS-IMPORT and S-SHELL notes.
- [ ] `docs/coverage.md` if the reference workflow changes; mark Implemented.
