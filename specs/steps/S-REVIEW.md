# S-REVIEW: `review` gate step
Status: Implemented
Milestone: M1

## Context
`review` is the explicit human checkpoint in a workflow (architecture §3, step
table): the run stops at the gate until someone looks at the albums and decides
to continue or reject, e.g. before a `beets.command move` puts files in their
final place. Unattended use comes first (AGENTS.md non-negotiable 6): the gate
never needs someone watching while it waits. The decision arrives through the
review queue, possibly days later, and in M2 a notification announces it.

Pending ChangeSets are already gated by `apply: review` (C-CHANGESET
CHANGESET-7); this step doesn't collect or apply ChangeSets. It shows what the
run has done so far and asks for a decision. M1 has no lanes, so a gate pauses
the whole run; with lanes (M2) it pauses each matching album's lane, and the
decision becomes per album.

Author decisions (2026-11-11): the gate is an approval checkpoint (Continue /
Reject), not a batching point for ChangeSets. Reject stops the run by default;
`on_reject: undo` also undoes the run's applied ChangeSets.

## Behavior
- REVIEW-1: The step MUST accept `message` (a beets template evaluated per album, after `{{ }}` interpolation at step start), `show` (fields to display per album, default none), `on_reject` (`stop` | `undo`, default `stop`), and optional `timeout` (a duration such as `3d` or `12h`) with `on_timeout` (`continue` | `reject`, default `reject`). `on_timeout` without `timeout` MUST be a validation error.
- REVIEW-2: The gate MUST act on the step's album selection narrowed by `when` (RUN-2). If no album matches, the step MUST finish at once with "nothing to review" and not pause the run.
- REVIEW-3: Otherwise the step MUST create a review gate in the app DB, in state `pending`, holding for each matching album: its label, the evaluated `message`, the `show` field values, the run's changes to it so far (REVIEW-4), and whether this run imported it (the run marker, `rifferr_run`). It MUST emit `review.requested` with the gate id and album count, and finish, leaving the run suspended in `waiting_review` (RUN-14).
- REVIEW-4: "Changes so far" MUST be folded from the run's journal (C-JOURNAL): per album, item and field, the first `before` and the last `after` of applied entries that aren't undone, omitting fields that ended where they started. The run's `conflicted` ChangeSets for the album MUST be listed too.
- REVIEW-5: A template error in `message` MUST fail that album's target (RUN-5 `on_error`). Under `continue`, the gate is created for the other albums.
- REVIEW-6: The gate's content MUST be fixed when it is created, so the decision refers to what the reviewer saw. A gate MUST be decided exactly once: `continue` or `reject`, with an optional note and the deciding user or `timeout`. A second decision MUST fail with `409 already_resolved`. Deciding emits `review.resolved`.
- REVIEW-7: On `continue`, the run MUST resume at the next step (RUN-14). The step record shows the decision, note, who decided and when.
- REVIEW-8: On `reject` with `on_reject: stop`, the run MUST end `cancelled` with reason `rejected_at_review`, later steps don't run, and the run's `proposed` ChangeSets become `rejected`. With `on_reject: undo`, the worker MUST first undo the run's applied ChangeSets newest first (JOURNAL-6) and log what was undone and any conflict that stopped it; the run then ends `cancelled` with reason `rejected_at_review` either way. Imports are not undone (C-JOURNAL non-goal), and the log says so when the run imported albums.
- REVIEW-9: With `timeout`, the server MUST store the gate's deadline and, once it passes, decide the gate with `on_timeout` (decided by `timeout`), including after a server restart: an overdue gate is decided at start-up.
- REVIEW-10: Cancelling a run waiting at a gate MUST set the gate to `cancelled`, and it can no longer be decided.
- REVIEW-11: A pending gate MUST survive server and worker restarts. The run stays `waiting_review` and resumes normally when the gate is decided.
- REVIEW-12: The step MUST NOT write to the library: it requires only `query` and `templates` capabilities, and a `continue` decision needs no worker job (the server queues the resumed run). Only `on_reject: undo` runs a worker job.

### C-RUN amendment this spec needs
- RUN-14 (new; generalizes CHANGESET-7): A run MAY be suspended on *holds*: pending ChangeSets of an `apply: review` step (CHANGESET-7) and pending review gates (REVIEW-3). The states become `queued → running → (waiting_review → queued)* → (succeeded | failed | cancelled)`, plus `waiting_review → cancelled`. While suspended the run holds no worker. When its last hold is resolved and none ended the run, the server MUST queue it to resume at the step after the one that suspended it; earlier steps' outputs are restored from their step records. `run.waiting_review` carries the holds (ChangeSet ids and gate ids).

## Interfaces
```python
class ReviewParams(BaseModel):
    message: str | None = None          # beets template, per album
    show: list[str] = []
    on_reject: Literal["stop", "undo"] = "stop"
    timeout: Duration | None = None     # "30m", "12h", "3d"
    on_timeout: Literal["continue", "reject"] | None = None   # default reject when timeout set

GateState = Literal["pending", "continued", "rejected", "cancelled"]

class GateAlbum(BaseModel, frozen=True):
    target: TargetRef; label: str; message: str | None
    shown: dict[str, FieldValue]
    changes: list[FieldEffect]          # folded, REVIEW-4
    conflicted: list[str]               # ChangeSet ids
    imported_by_run: bool

class ReviewGate(BaseModel, frozen=True):
    id: str; profile: str; run_id: str; step_id: str
    state: GateState; albums: list[GateAlbum]
    created_at: datetime; deadline: datetime | None
    decided_at: datetime | None; decided_by: str | None   # "ui", "cli", "timeout"
    note: str | None
```
- App DB: `review_gates` (id, profile, run_id, step_id, state, body JSON, created_at, deadline, decided_at, decided_by, note).
- Events: `review.requested {gate_id, albums}`, `review.resolved {gate_id, decision, decided_by}`.
- The decision API (`POST /api/reviews/{id}/decision {decision, note?}`) and the review-queue screen belong to F-REVIEW-QUEUE.

Implementation notes (2026-11-12, added while implementing; not new behavior):
- `ReviewGate` also stores `on_reject` and `on_timeout`, so the server can act on a decision without reloading the workflow.
- Step API: `ctx.open_gate(requests: list[GateRequest], options: GateOptions) -> gate_id` (RUN-14 holds for any step type). `GateRequest` is `{album_id, message, shown}`; the engine adds label, changes, conflicted ChangeSets and the run marker check. Stores implement `engine.review.GateStore` (app DB: `AppReviewGates`).
- Server side: `WorkerSupervisor.decide_gate(gate_id, decision, by=, note=)` raises `GateResolvedError` (→ `409 already_resolved` in F-REVIEW-QUEUE), `KeyError` for an unknown gate, and `ValueError` while the run isn't suspended yet. `decide_overdue_gates()` runs at start-up and from a timer that sleeps until the next deadline (at most 60 s).
- `on_reject: undo` runs as worker job kind `review_reject`. Holds are released by `AppDB.requeue_if_released`, one transaction over ChangeSet holds and gates.
- The `review` step's outputs: `gate`, `albums`; after the decision also `decision`, `decided_by`, `decided_at`, `note` (folded from `review.resolved`, REVIEW-7). Durations: `core.duration.Duration` (`30m`, `12h`, `3d`, `1d12h`).

## Examples
```yaml
- id: check-compilations
  uses: review
  when: 'comp:1'
  with:
    message: 'Check $albumartist - $album before the files move'
    show: [albumartist, album, year, albumtype]
    on_reject: undo
- id: move
  uses: beets.command
  with: { command: move }
```
A run imports two albums, one a compilation, and a `tags.script` step changed its
`album` → the gate lists one album with its message, the four fields and
`album: "Hits" → "Hits (Compilation)"`; run → `waiting_review`. Reject → the
tag change is undone, the run ends `cancelled` (`rejected_at_review`), `move`
never runs, and the log notes the import itself stays. If neither album is a
compilation, the step logs "nothing to review" and `move` runs straight away.

## Extensibility
Other step types can raise holds the same way (RUN-14): e.g. M2 import decisions
and cover picks. The gate's content model is fixed.

## Security & safety
- The gate never writes tags; undo goes through the journal like any undo (C-JOURNAL).
- Decisions are mutating API calls behind the F-SECURITY middleware (F-REVIEW-QUEUE).
- `message` values are templates over the user's own tags, shown as text, never HTML.

## Non-goals
- Per-album decisions and lanes (M2).
- Notifications (S-NOTIFY, M2); this spec only emits `review.requested`.
- Editing tags at the gate. Edits are manual ChangeSets (F-TAG-GRID).
- Undoing imports on reject (C-JOURNAL non-goal).

## Open questions
- None yet.

## Tasks
- [x] `ReviewParams` + validation (timeout / on_timeout)
- [x] RUN-14 holds: `waiting_review`, resume after the suspending step, outputs restored
- [x] gate creation: selection, message, shown fields, journal fold, conflicted ChangeSets
- [x] app DB `review_gates` + decide once + events
- [x] reject: stop / undo; cancel while waiting
- [x] deadlines and timeout decisions, including at start-up
