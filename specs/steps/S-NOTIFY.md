# S-NOTIFY: `notify` step and notification rules
Status: Implemented
Milestone: M2

## Context
Rifferr's main use is unattended, trigger-started imports (architecture §2a, non-negotiable 6).
When an album needs a human, its lane waits in the review queue (C-LANES), and the user must
hear about it without watching the UI. Architecture §2a calls for "a built-in `notify` step and
global notification rules. Apprise covers ntfy, Gotify, Discord, email, Matrix, webhooks and
more. Default events: review needed, run failed, run summary. Notification links open the exact
item in the review queue." §3 lists `notify` as "Send a notification. Through Apprise URLs from
the profile; message is templated." M2's done-when needs it: "an ambiguous album waits in
review, sends a notification and doesn't block other albums."

Two parts, one delivery path:
- **Rules** (profile-level): the server watches the events it already persists (F-API, C-LANES)
  and sends notifications for matching ones. They work for every workflow, with no step needed.
- **The `notify` step** (workflow-level): sends a templated message at a chosen point in a
  workflow, for one run or once per album.

Both send through targets named in the profile, whose Apprise URLs are secrets (C-PROFILE),
because Apprise URLs carry tokens and passwords.

## Behavior
### Targets and delivery
- NOTIFY-1: `profile.yaml` MAY have a `notify` section with `targets`: a map from target name
  (`^[a-z0-9][a-z0-9_-]{0,63}$`) to the name of an entry in `secrets` (PROFILE-4) whose value is
  one or more Apprise URLs (separated by whitespace or commas). A target naming a secret that
  doesn't exist MUST be a validation error. The URL itself MUST NOT appear in `profile.yaml`.
- NOTIFY-2: A notification MUST have a `title`, a `body`, a `type` (`info`, `success`,
  `warning` or `failure`, mapped to Apprise's notify types) and an optional `url`. Delivery MUST
  go through one `Notifier` interface (Interfaces) whose built-in implementation uses Apprise.
  The secret is resolved at the point of sending (PROFILE-5).
- NOTIFY-3: Resolved Apprise URLs MUST NOT appear in events, logs, run records, API responses or
  error messages. Rifferr refers to a target only by its name. Apprise's own log output MUST be
  routed through the profile's `Redactor` (C-PROFILE) before it reaches any Rifferr log.
- NOTIFY-4: A failed send MUST be retried after each delay in `notify.retry` (a list of
  Durations, default `[5s, 30s, 2m]`; `[]` means no retries). Each final outcome MUST emit
  `notify.sent` or `notify.failed` (`target`, `source`: `rule` or `step`; `failed` also
  carries a redacted `error`). A step's events go on its run's stream. A rule's events go on
  the profile's stream, with `event` (the rule event) and `run_id` when there is a run,
  because a running run's stream has one writer, its worker (B-037). Both reach
  `/api/events`.
- NOTIFY-5: A notification failure MUST NOT fail, block or delay a run, lane, gate or import,
  except as NOTIFY-12 allows for the step.
- NOTIFY-6: When the profile sets `notify.base_url` (an absolute `http`/`https` URL, validated
  on load), notification links MUST be built from it: `/review?item=<id>` for one queue item,
  `/review?run=<run_id>` for several items from one run, and `/runs/<run_id>` for run events.
  Without `base_url`, notifications MUST carry no `url`, and the body says to open Rifferr. A
  link MUST NOT contain the local token or any other credential (F-SECURITY).

### Rules
- NOTIFY-7: `notify.rules` MUST be a list of rules, each with `on` (one or more rule events,
  NOTIFY-8), `targets` (target names, default all) and optional `workflows` (workflow names; when
  set, the rule only matches runs of those workflows). If `targets` are set and `rules` is
  omitted, the default rules MUST apply: `review.needed`, `run.failed` and `run.summary` to all
  targets. `rules: []` turns rule notifications off.
- NOTIFY-8: The rule events MUST be:
  - `review.needed`: one or more queue items were created for a run: review gates
    (`review.requested`), import decisions (`import.requested`) or ChangeSets held by an
    `apply: review` step. Items created in the same pass of the same step MUST be sent as one
    notification with their count and album labels (at most 10 listed, then "and N more").
    The link is the single item, or the run's items when there are several (NOTIFY-6).
  - `run.failed`: a run reached `failed` (LANES-9). The body names the failing step and the
    first error line; the type is `failure`.
  - `run.summary`: a run stopped running, that is it reached a final state or `waiting_review`
    (LANES-7). The body counts lanes by state (done, waiting, failed, rejected, gone). A run
    with no lanes MUST NOT send a summary unless the rule sets `include_empty: true`.
  - `review.timeout`: a gate or import decision was decided by its timeout (REVIEW-9,
    F-IMPORT-UI). Not in the defaults.
  - `worker.problem`: a profile's worker crashed, stopped answering, or refused to start
    (F-WORKER). It has no run; the body names the profile and the reason, the link is the
    dashboard (`/`), and the type is `failure`. Not in the defaults. Idle exits never match.

  `review.needed` and `run.summary` MUST stay separate notifications, also when one transition
  causes both.
- NOTIFY-9: Rules MUST be evaluated in the server, from events as they are persisted, so that
  they also cover events the server itself emits (gate timeouts, worker crashes). Each rule
  event of a run MUST be sent at most once per target for the same pass and transition, also
  when events are replayed after a server restart: sent notifications are recorded in the app
  DB (`notifications`) before they are sent, and a pending record found at start-up is retried.
- NOTIFY-16 (F-REVIEW-QUEUE amendment): `GET /api/review` MUST accept `run=<run_id>` to list only
  that run's items, and `/review?run=<run_id>` MUST open the queue filtered to that run, with a
  visible control that clears the filter. An unknown run id shows an empty queue, not an error.
  (Already true since F-REVIEW-QUEUE: REVIEW-QUEUE-16 and its "Show every run" link.)
- NOTIFY-15: A rule with `review.needed` MAY set `remind_after` (a Duration). While any item of
  an earlier `review.needed` notification is still pending, the server MUST send a reminder
  for it every `remind_after`, at most `remind_limit` times (default 3). The reminder lists only
  the items still pending, says how long they have waited, and is recorded and deduplicated
  like any rule notification (NOTIFY-9), so reminders survive a server restart. Deciding,
  cancelling or timing out the last item stops its reminders.
- NOTIFY-10: Rule messages MUST use built-in title and body text that names beets concepts
  plainly (album, query) and shows the workflow name, profile and album labels.

### The `notify` step
- NOTIFY-11: The step MUST accept `targets` (default all), `title`, `body`, `type` (default
  `info`), `per` (`run` or `album`, default `run`) and `link` (`run`, `review` or `none`, default
  `run`). With `per: run`, `title` and `body` are `{{ }}` interpolated at step start (WORKFLOW-8)
  and the step is `run_level` (LANES-6): it sends once per pass, also with no lanes, unless
  `when:` matches no album and `skip_empty: true` is set. With `per: album`, `title` and `body`
  are beets templates evaluated per album after `{{ }}` interpolation, as in REVIEW-1, and one
  notification is sent per album in the pass's selection narrowed by `when:`. A template error
  fails that album's target (RUN-5).
- NOTIFY-12: The step MUST wait for the send result (NOTIFY-4 retries included) and record per
  target `sent` or `failed` in its outputs (`sent`, `failed`: lists of target names). A failure
  MUST be logged as a warning and the step still succeeds, unless `required: true` is set, in
  which case the step fails for the run (no target) and RUN-5 `on_error` applies.
- NOTIFY-13: The step MUST NOT read secrets other than its targets', MUST NOT write to the
  library and requires only the `query` and `templates` capabilities (for `per: album`). It runs
  in trusted and untrusted workflows alike, because it can only reach targets the profile owner
  configured. Unknown target names MUST be a validation error at workflow validation.

### Testing a target
- NOTIFY-14: `POST /api/profiles/{profile}/notify/test {target}` and `rifferr notify test
  <target> [--profile]` MUST send a fixed test notification to that target and answer with
  `sent` or `failed` plus the redacted error. The route is a mutating JSON endpoint under
  F-SECURITY's checks.

## Interfaces
```yaml
# profile.yaml
secrets:
  ntfy_phone: env:RIFFERR_NTFY_URL          # value: "ntfys://user:pass@ntfy.example/music"
  discord: keyring:rifferr/discord
notify:
  base_url: https://rifferr.home.lan
  retry: [5s, 30s, 2m]                      # default
  targets:
    phone: ntfy_phone
    discord: discord
  rules:                                    # omit for the defaults (NOTIFY-7)
    - on: [review.needed, run.failed, worker.problem]
      targets: [phone]
      remind_after: 1d
    - on: run.summary
      targets: [discord]
      workflows: [inbox]
```
```python
NotifyType = Literal["info", "success", "warning", "failure"]
RuleEvent = Literal["review.needed", "run.failed", "run.summary", "review.timeout",
                    "worker.problem"]

class Notification(BaseModel, frozen=True):
    title: str; body: str; type: NotifyType = "info"; url: str | None = None

class NotifyRule(BaseModel):
    on: list[RuleEvent]                       # a single string is accepted
    targets: list[str] | None = None          # None = all targets
    workflows: list[str] | None = None
    include_empty: bool = False               # run.summary only
    remind_after: Duration | None = None      # review.needed only (NOTIFY-15)
    remind_limit: int = 3

class NotifySettings(BaseModel):
    base_url: AnyHttpUrl | None = None
    retry: list[Duration] = ["5s", "30s", "2m"]
    targets: dict[str, str] = {}              # target name -> secrets key
    rules: list[NotifyRule] | None = None     # None = defaults

class SendResult(BaseModel, frozen=True):
    target: str; ok: bool; error: str | None  # error already redacted

class Notifier(Protocol):
    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult: ...

class NotifyParams(BaseModel):
    targets: list[str] | None = None
    title: str; body: str
    type: NotifyType = "info"
    per: Literal["run", "album"] = "run"
    link: Literal["run", "review", "none"] = "run"
    required: bool = False
    skip_empty: bool = False
```
- App DB: `notifications` (id, profile, run_id, event, dedup_key, target, state
  `pending|sent|failed`, attempts, message JSON, items JSON, remind_after, remind_limit,
  reminders, reminding, parent, created_at, sent_at, last_reminded_at, error).
- Events: `notify.sent {target, source, event?, run_id?, notification_id?}`,
  `notify.failed {..., error}` (NOTIFY-4).
- Routes: `POST /api/profiles/{profile}/notify/test`; `GET /api/review?run=` (NOTIFY-16). CLI: `rifferr notify test`.
- Module: `rifferr.notify` (models, rule matching, message building: pure) and
  `rifferr.notify.apprise` (the only module that imports Apprise).

Implementation notes (2026-11-14, added while implementing; not new behavior):
- Pure parts live in `rifferr.core.notify` (models, `NotifySettings` in `ProfileFile.notify`,
  links, rule message text), so `core.profile` can use them. `rifferr.notify` has `Notifier`,
  `deliver` (retries, redaction) and `ProfileTargets`; `rifferr.notify.service` the server's
  `NotificationService`; `rifferr.notify.apprise` the `AppriseNotifier`.
- Step API: `ctx.notify(notification, targets=None) -> list[SendResult]`, plus
  `ctx.notify_targets` and `ctx.base_url`. The worker passes `ProfileTargets` to
  `Engine.create_run(notify=...)`; the engine emits the step's `notify.*` events.
- NOTIFY-13 validation is general: a step parameter marked
  `json_schema_extra={"x-rifferr-ref": "notify-target"}` is checked against the profile's
  targets by `validate(..., notify_targets=...)`, which `create_run` passes. A workflow is
  therefore checked when a run is created (like profile vars), not by `rifferr validate`.
- Step outputs are scalars (C-RUN), so `sent` and `failed` are comma-separated target names;
  `messages` counts the notifications sent.
- `review.needed` groups the `lane.waiting` events of a pass: they are flushed by the run's
  next non-lane event, or after 1 s. Its dedup key is the run plus the sorted hold refs.
  Summaries and failures use the triggering event's seq.
- `worker.problem` comes from `WorkerSupervisor.subscribe_problems`: a worker that wasn't
  ready, was killed as hung, or exited non-zero, outside server shutdown.
- Apprise's `apprise` logger doesn't propagate; its lines are captured per send and redacted
  (the URLs and their parts) before they reach Rifferr's log at debug level.
- `rifferr notify test` sends from the CLI process and needs no server.

## Examples
```yaml
# a workflow ending with a per-album message
- id: tell
  uses: notify
  when: "rifferr_run::^{{ run.id }}$"
  with:
    per: album
    targets: [phone]
    title: "Imported: $albumartist – $album"
    body: "$year · $label · %if{$comp,compilation,}"
```
- Inbox run, 12 albums, 2 go to import decisions in the `import` step's first pass
  → one `review.needed`: "2 albums need a decision (inbox, profile main): A – X, B – Y",
  link `https://rifferr.home.lan/review?run=<id>`. When the other 10 finish and the run is
  `waiting_review` → `run.summary`: "inbox: 10 done, 2 waiting for review".
- Nightly schedule finds nothing → no lanes → no summary (defaults).
- ntfy is down → 3 retries, `notify.failed {target: phone, source: rule}`; the import is
  unaffected (NOTIFY-5).

## Extensibility
- Any service Apprise supports works by adding its URL as a secret; Apprise's custom
  notification plugins (its `@notify` decorator and plugin path) work the same way.
- The rule events and the built-in message text stay fixed in this spec. The `Notifier`
  interface is injected, so tests and future providers don't depend on Apprise.

## Security & safety
- Apprise URLs are secrets (NOTIFY-1, -3): never in YAML, logs, events or responses.
- Links carry no token (NOTIFY-6); opening one still goes through normal auth (F-SECURITY).
- Untrusted workflows can send only to targets the profile owner configured (NOTIFY-13), and
  only text they template. They can't add URLs.
- Message bodies can include album labels and tag values from imported files; they are sent as
  plain text (Apprise `body_format: text`), never as HTML or Markdown.

## Non-goals
- Quiet hours, digests or rate limits beyond NOTIFY-8's per-pass grouping and NOTIFY-15's
  reminder limit.
- Custom rule message templates (step messages are templated; rule messages are fixed).
- Desktop notifications (`notify-send`); Apprise's desktop backends are not set up or tested.
- Replies or actions from the notification (accepting a review from ntfy).

## Open questions
Resolved (author, 2026-11-14):
1. Apprise approved (`>=2.2,<3`, licenses checked; dependency ledger).
2. `run.summary` and `review.needed` from the same transition stay two notifications (NOTIFY-8).
3. B-019 is covered by the opt-in `worker.problem` rule event (NOTIFY-8), left to the agent's
   judgment. Idle exits are excluded: they are normal.
4. The run filter is added here (NOTIFY-16).
5. Retry delays are a profile setting, `notify.retry` (NOTIFY-4).
6. Reminders for pending reviews: `remind_after` / `remind_limit` on rules (NOTIFY-15).

## Tasks
- [x] `rifferr.notify`: models, `NotifySettings` in `profile.yaml`, validation (NOTIFY-1, -6, -7)
- [x] `Notifier` protocol, Apprise implementation, redaction, retries (NOTIFY-2 to -5)
- [x] Rule engine in the server: event matching, grouping, dedup and the `notifications` table,
      restart recovery (NOTIFY-8 to -10)
- [x] `notify` step (NOTIFY-11 to -13)
- [x] Reminders (NOTIFY-15) and `worker.problem` from the supervisor (NOTIFY-8)
- [x] `GET /api/review?run=` and the `/review?run=` filter (NOTIFY-16): already built by
      F-REVIEW-QUEUE and tested there (vitest `REVIEW_QUEUE_16`); an API test added here. No
      new Playwright test, since the UI didn't change.
- [x] Test route and `rifferr notify test` (NOTIFY-14)
- [x] Contract test for the Apprise implementation using a custom Apprise schema (no network)
- [x] Regenerate schemas and the TS client. `docs/coverage.md` unchanged: the reference
      checklist has no notification step.
