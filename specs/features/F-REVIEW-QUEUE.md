# F-REVIEW-QUEUE: Review queue
Status: Approved
Milestone: M1

## Context
Albums that need a decision wait in the review queue without blocking anything else
(architecture §2a, non-negotiable 6). In M1 the queue holds two kinds of item, both
already stored and decidable from code:

- ChangeSets proposed with `apply: review` (C-CHANGESET CHANGESET-6, -7), including ones
  that `conflicted` when accepted (CHANGESET-8);
- `review` gates (S-REVIEW).

Decisions reach the worker as jobs (CHANGESET-13) through `WorkerSupervisor.decide()` /
`decide_gate()`; this spec adds their API and the screen, plus run undo (C-JOURNAL
JOURNAL-6), whose API and UI C-JOURNAL leaves to this spec. The queue *dashboard* that
becomes the home screen (inbox status, runs in progress, recent imports) is M2; this is
the M1 queue page it will grow from. Import decisions and cover picks join the queue in M2/M3.

## Behavior
- REVIEW-QUEUE-1: `GET /api/review?profile=&run=` MUST list what waits for a decision, oldest first: ChangeSets in state `proposed` or `conflicted` that belong to a run, and `pending` review gates. Each item carries its run id, workflow name, step id and profile. Manual ChangeSets never wait here (F-TAG-GRID applies them at once).
- REVIEW-QUEUE-2: `GET /api/changesets/{id}` MUST return a ChangeSet with its state, per-field-change decisions (CHANGESET-9), `resolved_by`, and its journal entry ids.
- REVIEW-QUEUE-3: `GET /api/changesets/{id}/current` MUST answer from the profile's read-only worker (WORKER-11) with each target's label and the current values of the fields the ChangeSet changes, or `missing` for a target that no longer exists. It MUST NOT write anything.
- REVIEW-QUEUE-4: `POST /api/changesets/{id}/decision {decision: accept|reject, accepted?, note?}` MUST queue a decision job (CHANGESET-13) and answer `202 {job_id}`. `accepted` is the list of field-change indexes to accept (CHANGESET-9); absent = all. Unknown ChangeSet: `404`. Already resolved: `409 already_resolved`. Accepting a conflicted one: `409 conflicted`. Bad indexes: `400 bad_request`.
- REVIEW-QUEUE-5: `POST /api/changesets/{id}/repropose` on a `conflicted` ChangeSet MUST queue a worker job that proposes the same changes again as a new ChangeSet of the same run and step, with each `old` taken from the current values (CHANGESET-8). The old ChangeSet becomes `rejected` with `resolved_by` naming the new one, and the run's holds swap the old id for the new one in the same transaction, so the run keeps waiting (open question 1). Field changes that are no-ops on the current values are dropped (CHANGESET-4); if none is left, the old ChangeSet is rejected and nothing new is proposed.
- REVIEW-QUEUE-6: `POST /api/reviews/{gate_id}/decision {decision: continue|reject, note?}` MUST decide the gate (S-REVIEW REVIEW-6 .. -8) and answer `200` with the gate. Unknown: `404`; already decided or cancelled: `409 already_resolved`; run not suspended yet: `409 not_waiting`.
- REVIEW-QUEUE-7: `POST /api/runs/{id}/decisions {decision: accept|reject}` MUST decide every `proposed` ChangeSet of that run the same way, one decision job each, and answer `202` with the job ids. Conflicted ChangeSets are left alone.
- REVIEW-QUEUE-8: `POST /api/runs/{id}/undo` MUST queue the run undo job (JOURNAL-6) and answer `202 {job_id}`; `409` while the run is queued or running or has nothing to undo. `POST /api/profiles/{profile}/journal/{entry_id}/undo` (F-TAG-GRID) also undoes entries of runs.
- REVIEW-QUEUE-9: Decisions record `decided_by` / `resolved_by` = `ui` (open question 8) and the note, if given.
- REVIEW-QUEUE-10: `GET /api/events` (API-9) MUST also carry `run.waiting_review`, `run.resumed`, `review.requested`, `review.resolved` and the `changeset.*` events of runs, so the queue updates without polling (open question 3). Undo and decision job results arrive as the runs' `changeset.*` events.
- REVIEW-QUEUE-11: The page `/review` (nav item "Review", with the count of waiting items) MUST list the queue grouped by run, newest run first, with a link to each run.
- REVIEW-QUEUE-12: A ChangeSet MUST show, per target (by label) and field: the value the step saw, the proposed value, and the current value from REVIEW-QUEUE-3, marking fields whose current value differs. Each field change has a checkbox (all on by default); "Accept selected" accepts the checked ones (CHANGESET-9), "Reject" rejects all. A conflicted ChangeSet shows expected, current and proposed values with "Propose again on current values" and "Reject".
- REVIEW-QUEUE-13: A review gate MUST show per album: label, message, the `show` fields, the folded changes (REVIEW-4), conflicted ChangeSets and whether the run imported it, plus the deadline and its `on_timeout`. "Continue" and "Reject" take an optional note; "Reject" says what `on_reject` will do (stop the run, or undo its changes first).
- REVIEW-QUEUE-14: "Accept all" / "Reject all" per run (REVIEW-QUEUE-7) MUST ask for confirmation with the number of ChangeSets.
- REVIEW-QUEUE-15: A run page MUST show "waiting for review" with a link to its items in the queue while the run is `waiting_review`, and "Undo this run" for a finished run with applied, not undone ChangeSets. Undo asks for confirmation (it stops at the first conflict, JOURNAL-6) and then shows what was undone and the conflict that stopped it.
- REVIEW-QUEUE-16: `/review?item=<id>` MUST open and scroll to that ChangeSet or gate (M2 notifications link to it, architecture §2a).
- REVIEW-QUEUE-17: Values and messages MUST be shown as text, never as HTML.
- REVIEW-QUEUE-18: A Playwright test MUST cover, against the e2e temp profile: a run whose `tags.script` step proposes for review and then a `review` gate; partially accept the ChangeSet in the queue; continue the gate; see the run finish; undo the run from its page.

## Interfaces
```python
class QueueItem(BaseModel):
    kind: Literal["changeset", "gate"]
    id: str
    profile: str; run_id: str; workflow: str | None; step_id: str | None
    created_at: datetime
    changeset: ChangeSet | None; state: ChangeSetState | None    # kind changeset
    gate: ReviewGate | None                                       # kind gate

class ChangeSetDetail(BaseModel):
    changeset: ChangeSet; state: ChangeSetState
    decisions: dict[int, bool]; resolved_by: str | None
    journal_ids: list[str]

class CurrentTarget(BaseModel):
    target: TargetRef; label: str; missing: bool
    fields: dict[str, FieldValue]       # only the fields the ChangeSet changes

class Decision(BaseModel):
    decision: Literal["accept", "reject"]
    accepted: list[int] | None = None
    note: str | None = None

class GateDecisionBody(BaseModel):
    decision: Literal["continue", "reject"]
    note: str | None = None
```
Routes: `GET /api/review`, `GET /api/changesets/{id}`, `GET /api/changesets/{id}/current`,
`POST /api/changesets/{id}/decision`, `POST /api/changesets/{id}/repropose`,
`POST /api/reviews/{gate_id}/decision`, `POST /api/runs/{id}/decisions`,
`POST /api/runs/{id}/undo`. Read-only worker request kind `changes.current`.
Job kind `repropose`. UI routes: `/review`, `/review?item=`.

## Examples
A run's `tags.script` step (`apply: review`) proposes `album: Live → Live (EP)` and
`albumdisambig: → EP` for one album; the run is `waiting_review`. In `/review` the user
unticks `albumdisambig` and clicks "Accept selected" → decision job with `accepted: [0]`
→ `changeset.applied` (partial) → the run resumes → its `review` gate opens and shows
the album with the folded change → "Continue" → the run finishes.

Between proposal and decision someone sets the album to `Live!` → accepting gives
`changeset.conflicted` → "Propose again on current values" → a new ChangeSet
`Live! → Live (EP)` replaces it in the run's holds.

## Extensibility
- Queue item kinds are a tagged union, so import decisions (F-IMPORT-UI) and cover picks
  (F-COVER-PICKER) add kinds without changing the existing ones.
- Current values come from the backend's snapshots, so any backend with `query` works.

## Security & safety
- Every decision route is a mutating JSON route behind the F-SECURITY middleware.
- Decisions go through the worker; the API server never applies anything (CHANGESET-13).
- Values are text only (REVIEW-QUEUE-17).

## Non-goals
- The M2 queue dashboard on `/` (inbox status, runs in progress, recent imports).
- Editing a proposed value before accepting (C-CHANGESET non-goal; accept, then fix in the tag grid).
- Import decisions, cover picks and AI suggestions in the queue (M2, M3, M4).
- Notifications (S-NOTIFY, M2).
- A `rifferr review` CLI (open question 7).
- Users and permissions: `ui` is the only decider in M1.

## Open questions
1. Re-propose (REVIEW-QUEUE-5): CHANGESET-8 allows it but doesn't say how the run's holds
   follow. Resolved (approved 2026-11-13): as recommended — the conflicted ChangeSet becomes `rejected` (resolved_by
   `re-proposed as <id>`) and the new one takes its place in the run's holds in one
   transaction, so the run waits for the new one.
2. Scope of the page: M1 adds `/review` only; `/` stays Runs until the M2 dashboard.
   Resolved (approved 2026-11-13): as recommended — yes.
3. Live updates: extend `GET /api/events` (API-9) with review-level and `changeset.*`
   events of runs (additive, compatible), or add a separate `/api/review/events` stream?
   Resolved (approved 2026-11-13): as recommended — extend `/api/events`.
4. Editing a proposed value before accepting: keep it a non-goal for M1? Resolved (approved 2026-11-13): as recommended — yes.
5. "Accept all" / "Reject all" per run (REVIEW-QUEUE-7, -14): include in M1?
   Resolved (approved 2026-11-13): as recommended — yes, with confirmation.
6. Run undo on the run page (REVIEW-QUEUE-15) rather than in the queue? Resolved (approved 2026-11-13): as recommended — yes.
7. CLI (`rifferr review list`, `accept`, `reject`): defer to a backlog entry?
   Resolved (approved 2026-11-13): as recommended — defer.
8. `decided_by`: there are no user accounts in M1, so record `ui` for every UI decision
   (S-REVIEW already uses `ui`, `cli`, `timeout`). Resolved (approved 2026-11-13): as recommended — `ui`.

## Tasks
- [ ] read-only worker: `changes.current`
- [ ] routes: queue, ChangeSet detail and current values, decisions, re-propose, gate decision, run decisions, run undo
- [ ] `repropose` worker job with the holds swap
- [ ] `/api/events` carries review-level events
- [ ] `/review` page: ChangeSet and gate cards, partial accept, bulk per run, deep links
- [ ] run page: waiting link, undo run
- [ ] Playwright test (REVIEW-QUEUE-18)
