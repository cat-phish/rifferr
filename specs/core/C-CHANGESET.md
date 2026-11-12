# C-CHANGESET: ChangeSet model, apply policy and review states
Status: Approved
Milestone: M1

## Context
Every tag change is a reviewable ChangeSet (architecture tenet 3, §2; AGENTS.md
non-negotiable 3). Tag scripts, `shell` steps with `emits: changes`, AI steps,
chat and manual edits all produce ChangeSets; only the engine applies them,
through `LibraryBackend.apply` (§8), after the journal has recorded the intent
(C-JOURNAL). This spec defines the model, how steps propose ChangeSets, the
step `apply` policy (C-WORKFLOW WORKFLOW-3), the states a ChangeSet moves
through, and the backend side of applying. C-BACKEND BACKEND-9 left
`ChangeSet` empty and `apply` unimplemented until now.

Amended 2026-11-11 with the S-SHELL M1 update (author): short form, split per album (CHANGESET-3, -14).

Author decisions (2026-11-11): apply writes file tags (a profile can opt out);
in M1 `apply: review` suspends the whole run (per-album lanes arrive in M2);
a field that changed since it was proposed is a conflict that stops the apply;
review may accept a subset of a ChangeSet's field changes.

## Behavior
- CHANGESET-1: A `ChangeSet` MUST hold an `id` (ULID), `profile`, `run_id` and `step_id` (both null for manual edits), `source` (`step` | `manual` | `chat` | `ai`, plus a free-form `name`, e.g. the step type), an optional `rationale`, `created_at`, and a non-empty list of `TargetChange`s. A `TargetChange` names one album or item by its backend id and scope and holds a non-empty list of `FieldChange`s. A `FieldChange` holds `field`, `op` (`set` | `delete`), `old` (the value the proposer saw, `null` if the field was absent), `new` (for `set`) and an optional per-field `rationale`.
- CHANGESET-2: Field names MUST match `^[a-z_][a-z0-9_]*$`. A backend MUST reject changes to fields it treats as read-only (for beets: `id`, `path`, `album_id`, `mtime`, `added` and other computed fields), and values whose type the field can't store (e.g. a non-integer `year`), through `LibraryBackend.check(changes) -> list[ChangeProblem]`, before anything is applied.
- CHANGESET-3: Steps MUST propose ChangeSets only through `ctx.propose(proposal)`, never by writing to the library. A `ChangeSetProposal` (published JSON Schema `schemas/changeset-proposal.schema.json`, also the stdout format of `shell` steps with `emits: changes`) holds either `targets` or, exclusively, the short form `changes`, which `ctx.propose(proposal, current=…)` resolves to the step's current album or item (rejected without one). `old` MAY be omitted, and the engine fills it from the snapshot the step was given. The engine adds `id`, `profile`, `run_id`, `step_id`, `source` and `created_at`.
- CHANGESET-4: The engine MUST drop field changes whose `new` equals the current value (and `delete`s of absent fields), and MUST NOT create a ChangeSet that ends up empty. So re-running an idempotent script proposes nothing.
- CHANGESET-5: Every ChangeSet MUST be persisted in the app DB with a state: `proposed` → `applied`, `rejected`, `partially_applied` or `conflicted`; `applied` and `partially_applied` → `undone` (C-JOURNAL). Each transition MUST emit an event (`changeset.proposed`, `changeset.applied`, `changeset.rejected`, `changeset.conflicted`, `changeset.undone`) on the run's event stream, or on the profile's stream for manual edits.
- CHANGESET-6: With the step's `apply: auto`, the engine MUST apply each proposed ChangeSet as soon as the step proposes it. With `apply: review` (the default), the ChangeSets stay `proposed` in the review queue. AI steps MUST default to `review` regardless (AGENTS.md non-negotiable 4); a step type MAY declare `force_review = True`, which makes `apply: auto` a validation error for it.
- CHANGESET-7: When a step with `apply: review` finishes and has proposed at least one ChangeSet, the run MUST be suspended: run state `waiting_review` (new in C-RUN), job finished, worker released, a `run.waiting_review` event emitted with the pending ChangeSet ids. When the last of those ChangeSets is resolved (accepted, partly accepted, rejected or conflicted and then resolved), the server MUST queue the run to resume at the next step. Earlier steps' outputs are restored from their stored step records. A run cancelled while `waiting_review` is `cancelled`, and its pending ChangeSets are `rejected`.
- CHANGESET-8: Applying MUST be all-or-nothing per ChangeSet in the library DB. Before writing, the backend MUST compare each `old` with the current value; on any difference it MUST apply nothing and return the conflicts, and the engine marks the ChangeSet `conflicted` (in review, showing expected, current and proposed values). From review, a conflicted ChangeSet can be rejected, or re-proposed against the current values as a new ChangeSet.
- CHANGESET-9: Review MUST be able to accept a subset of a ChangeSet's field changes. The accepted subset is applied as one unit (CHANGESET-8) and journaled; the rest are recorded as rejected; the ChangeSet becomes `partially_applied`.
- CHANGESET-10: `BeetsBackend.apply` MUST store album-scoped changes on the album (so beets carries album-level fields to its items) and item-scoped changes on the item, inside one library transaction. It MUST then write the tags to the files with beets' own writer, unless the profile sets `changes.write_tags: false`. A failed file write MUST NOT undo the DB change; it MUST be reported per item and journaled (C-JOURNAL), and shown in the run log.
- CHANGESET-11: `apply` MUST report what actually changed, including item fields that beets changed because of an album-level change, so undo restores exactly that (C-JOURNAL).
- CHANGESET-12: `FakeBackend` MUST implement `check` and `apply` with the same semantics in memory, including conflicts and the album-to-item propagation for a configurable set of album-level fields. The backend contract suite MUST cover both backends.
- CHANGESET-14: `ctx.propose` MUST split a proposal into one ChangeSet per album. Each holds the album's own target (if any) and the targets of its items, grouped by the snapshot's album membership, in the order they appear, and keeps the proposal's `rationale`. `propose` returns the ids it created (empty when everything was a no-op, CHANGESET-4).
- CHANGESET-13: Only the engine (in the worker) MUST call `LibraryBackend.apply`. Review decisions from the API MUST reach it as jobs, like runs (F-WORKER WORKER-3).

## Interfaces
```python
FieldOp = Literal["set", "delete"]

class FieldChange(BaseModel, frozen=True):
    field: str                  # ^[a-z_][a-z0-9_]*$
    op: FieldOp = "set"
    old: FieldValue = None      # what the proposer saw; None = absent
    new: FieldValue = None      # required for "set"
    rationale: str | None = None

class TargetRef(BaseModel, frozen=True):
    scope: Scope                # "album" | "item"
    id: str                     # backend id from the snapshot

class TargetChange(BaseModel, frozen=True):
    target: TargetRef
    changes: list[FieldChange]  # min 1

class ChangeSource(BaseModel, frozen=True):
    kind: Literal["step", "manual", "chat", "ai"]
    name: str                   # e.g. "tags.script", "ui"

ChangeSetState = Literal["proposed", "applied", "partially_applied", "rejected",
                         "conflicted", "undone"]

class ChangeSet(BaseModel, frozen=True):
    id: str; profile: str; run_id: str | None; step_id: str | None
    source: ChangeSource; rationale: str | None; created_at: datetime
    targets: list[TargetChange] # min 1

class ChangeSetProposal(BaseModel, frozen=True):   # published schema
    rationale: str | None = None
    targets: list[ProposedTargetChange] | None = None  # FieldChange with optional `old`
    changes: list[ProposedFieldChange] | None = None   # short form: current target; exactly one of the two

class ChangeProblem(BaseModel, frozen=True):       # check() result
    target: TargetRef; field: str; message: str

class Conflict(BaseModel, frozen=True):
    target: TargetRef; field: str; expected: FieldValue; current: FieldValue

class ApplyResult(BaseModel, frozen=True):
    applied: bool
    conflicts: list[Conflict]
    effects: list[FieldEffect]   # C-JOURNAL: target, field, before, after
    write_failures: list[WriteFailure]

# LibraryBackend additions (C-BACKEND):
def check(self, changes: ChangeSet) -> list[ChangeProblem]: ...
def apply(self, changes: ChangeSet) -> ApplyResult: ...      # was -> JournalEntry
# StepContext addition (C-RUN):
def propose(self, proposal: ChangeSetProposal, current: TargetRef | None = None) -> list[str]: ...  # one id per album
```
- Profile setting: `changes: { write_tags: true }`.
- Step type attribute: `force_review: bool = False`.
- App DB (migration): `changesets` (id, profile, run_id, step_id, state, body JSON, created_at, resolved_at, resolved_by); `changeset_decisions` (changeset id, field-change index, accepted).

## Examples
A `tags.script` step with `apply: review` proposes for album `album-7`:
```json
{"targets": [{"target": {"scope": "album", "id": "album-7"},
  "changes": [{"field": "album", "old": "Live Sessions", "new": "Live Sessions (EP)"}]}]}
```
→ `changeset.proposed`; the step finishes; run → `waiting_review`. The user
accepts → job → worker applies (album stored, beets carries `album` to the
items, files written) → `changeset.applied` → run resumes at the next step.

If `album` was edited to "Live Sessions!" in between → nothing applied,
`changeset.conflicted` with expected "Live Sessions" and current "Live Sessions!".

Re-running the same script after it was applied → `new` equals current → no
ChangeSet (CHANGESET-4), so the step proposes nothing and the run doesn't pause.

## Extensibility
- Step plugins propose through `ctx.propose`; they can't apply.
- Backends implement `check` and `apply`; their read-only fields and type rules are theirs.
- Step types may set `force_review`.
- Fixed: the model, the states, the conflict rule and the engine-only apply path.

## Security & safety
- No code path except the engine's apply writes tags (non-negotiable 3); the backend never applies a ChangeSet the journal hasn't recorded (C-JOURNAL).
- AI output can never auto-apply (CHANGESET-6).
- Values are data: they are never evaluated as beets templates when applied. Templates are evaluated by the proposing step (e.g. `tags.script`), and the result is the literal `new`.

## Non-goals
- File operations (move, rename, delete) in ChangeSets. Moving files stays a `beets.command move` step (S-BEETS-COMMAND).
- Per-album lanes (M2): until then `apply: review` suspends the whole run.
- Editing a proposed value in review before accepting (F-REVIEW-QUEUE may add it as a manual ChangeSet).
- Merging two pending ChangeSets that touch the same field (the second conflicts once the first applies).

## Open questions
- None yet.

## Tasks
- [ ] models in `rifferr.core.changeset` + published proposal schema
- [ ] `ctx.propose`, empty/no-op dropping, events
- [ ] app DB tables + states; review decisions as worker jobs
- [ ] `waiting_review` suspend and resume (C-RUN amendment)
- [ ] `FakeBackend.check/apply` + contract tests
- [ ] `BeetsBackend.check/apply` (album/item store, file writes, `write_tags` opt-out) + contract tests
- [ ] `force_review` on step types
