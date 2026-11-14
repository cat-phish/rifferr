# F-TRIGGERS: Watch, schedule and webhook triggers; dedup; concurrency
Status: Draft
Milestone: M2

## Context
Unattended imports are the main use (non-negotiable 6, architecture §2a). Up to M1 every
run starts by hand (`rifferr run`, the UI). The workflow's `on:` block is parsed and
validated but ignored (C-WORKFLOW WORKFLOW-13). This spec makes the server act on it:

- **watch:** a folder is watched, and each new download in it starts a run once it has
  stopped changing (*settle*).
- **schedule:** a cron expression in the profile's time zone (C-PROFILE `timezone`).
- **webhook:** `POST` to an authenticated endpoint. Download managers (Lidarr Connect,
  slskd) are the first documented callers, but nothing in the engine knows about them.
- **dedup:** the same input with the same contents isn't started twice unless forced.
- **concurrency:** one active run per profile; further triggers queue.

Triggers live in the API server process (`rifferr serve`), not in the worker: workers stop
when idle (F-WORKER `worker.idle_timeout`), and a trigger only has to submit a run through
`WorkerSupervisor.submit_run`, which starts the worker. Lanes (C-RUN, next in M2) are out of
scope: a triggered run is an ordinary run with an input.

Neighbors: C-WORKFLOW (the `on:` block, the new `input` field and the `run.input` /
`run.trigger` context), F-SECURITY (SECURITY-3 already exempts webhook routes from the
Origin check; trust), F-WORKER (job queue), F-API (routes, events), F-CLI (`rifferr run
--input`, webhook secrets).

## Behavior

### Arming
- TRIGGERS-1: When the server starts, and whenever a workflow file in a profile's workflows directory is created, changed or deleted, the server MUST (re)arm that workflow's triggers from its current text. A workflow that fails validation arms nothing and reports `trigger.error`.
- TRIGGERS-2: Triggers of a workflow that is not trusted (SECURITY-10) MUST NOT be armed. Its trigger state is `disabled: untrusted` until it is trusted; trusting it arms them without a restart. A change to the file resets trust (SECURITY-10) and so disarms them (open question 7).
- TRIGGERS-3: `on.watch.path`, `on.webhook.within` and the workflow's `input` MUST be interpolated at arm time from the roots `vars` and `profile` only; a reference to `run` or `steps` in them MUST fail validation. A watch path that doesn't exist or isn't a directory gives state `error: path missing`, and the server retries arming it every minute without failing anything else.
- TRIGGERS-4: `GET /api/profiles/{profile}/triggers` MUST list every workflow's triggers with kind, state (`armed`, `disabled: untrusted`, `error: <message>`), the resolved watch path, the next schedule time, and the last time each fired with its run id.

### Run input
- TRIGGERS-5: Every run MUST carry `run.input` (a string, possibly empty) and `run.trigger` (`manual`, `watch`, `schedule` or `webhook`) in its interpolation context. WORKFLOW-8's roots are unchanged; these are new keys of `run`.
- TRIGGERS-6: A watch run's input is the settled folder's absolute path. A webhook run's input is the path taken from its payload (TRIGGERS-15). A schedule run's input, and a manual run's input when none is given, is the workflow's top-level `input` (resolved at arm time), or empty if it has none (open question 4).
- TRIGGERS-7: `rifferr run <workflow> --input PATH` and the UI's run dialog MUST accept an input path for manual runs.

### Watch
- TRIGGERS-8: The watcher MUST treat each top-level directory inside the watch path as one candidate input. Changes anywhere under that directory reset its settle timer. Loose files directly in the watch path MUST NOT start runs; the watcher emits one `trigger.skipped {reason: loose_files}` per settle (open question 5).
- TRIGGERS-9: A candidate MUST be submitted only after no change has been seen under it for the `settle` duration (`<n>s`, `<n>m` or `<n>h`; default `2m`). A candidate that disappears before settling, or that contains no files, is dropped silently.
- TRIGGERS-10: The watcher MUST use native file events by default and polling when `on.watch.poll` is set (`poll: 30s`) or `RIFFERR_FORCE_POLLING=1` is in the environment (open question 6). Poll intervals below `5s` MUST fail validation.
- TRIGGERS-11: On arming, the watcher MUST scan the watch path once and treat every existing top-level directory as a candidate with a fresh settle timer, so downloads that arrived while the server was down are picked up. Dedup (TRIGGERS-19) keeps this from starting folders that were already run.

### Schedule
- TRIGGERS-12: `on.schedule` MUST be a five-field cron expression, validated at load time. It fires in the profile's effective time zone (`profile_store.effective_timezone`). Firing times that DST skips don't fire; times that DST repeats fire once.
- TRIGGERS-13: Firings missed while the server was down MUST NOT be caught up; the next firing after start is the next one in the future (open question 9).
- TRIGGERS-14: A schedule firing while a run from the same workflow's schedule is still queued or running MUST be skipped with `trigger.skipped {reason: previous_still_active}`.

### Webhook
- TRIGGERS-15: `POST /hooks/{profile}/{name}` MUST start a run of the workflow whose `on.webhook.name` is `name` in that profile (open question 2). The body MUST be JSON (`Content-Type: application/json`) and at most 1 MiB; otherwise `415` / `413`. The input is read from the payload at the dotted path `on.webhook.input` (list indexes allowed: `trackFiles.0.path`). If that value is a file, the input is its parent directory.
- TRIGGERS-16: The resolved input path MUST be an existing directory whose real path (symlinks resolved) lies inside `on.webhook.within` (default: `on.watch.path` if set; one of the two is required when `on.webhook` is present). Otherwise the hook answers `422 bad_input` and starts nothing. A payload without a value at `on.webhook.input` answers `200 {ignored: "no_input"}` (so a caller's "test" event needs no special case).
- TRIGGERS-17: Each webhook MUST be authenticated by its own secret, separate from the UI login and the local token: `Authorization: Bearer <secret>` or HTTP Basic with the secret as the password (any username). The comparison MUST be constant-time. Missing or wrong secret: `401` with no detail. Webhook routes are exempt from the Origin / `Sec-Fetch-Site` check (SECURITY-3) but not from the Host allowlist (SECURITY-1, open question 10). Secrets MUST NOT appear in logs, events or error messages, and a secret in the URL query string MUST be rejected with `400`, so it never reaches access logs.
- TRIGGERS-18: A webhook that starts a run MUST answer `202 {run_id}`; one deduplicated away answers `200 {skipped: <reason>, run_id}` with the existing run.

### Dedup
- TRIGGERS-19: Before a triggered run is submitted, its input MUST be fingerprinted: SHA-256 over the sorted `(relative path, size, mtime_ns)` of every regular file under it (an empty input has no fingerprint and is never deduplicated). If the same workflow already has a run for the same input path and fingerprint that is queued, running, waiting for review or finished successfully, the trigger is skipped with `trigger.skipped {reason: in_progress | already_run, run_id}`.
- TRIGGERS-20: A failed or cancelled run does not block a new run of the same input once its fingerprint changes; with the same fingerprint it is skipped with `reason: previously_failed` (open question 8). `rifferr run --force` and the UI's "Run again" bypass dedup.
- TRIGGERS-21: Manual runs MUST NOT be deduplicated, but they record their fingerprint, so a later trigger for the same unchanged input is skipped.
- TRIGGERS-22: Dedup records live in the app DB per profile, workflow (by name) and input path, and MUST survive restarts.

### Concurrency and events
- TRIGGERS-23: At most one run per profile MUST be running at a time; triggered runs queue in submission order behind it (the F-WORKER job queue). A second trigger for an input that is already queued is skipped by dedup rather than queued twice.
- TRIGGERS-24: The server MUST emit `trigger.fired {workflow, kind, input, run_id}`, `trigger.skipped {workflow, kind, input, reason, run_id?}` and `trigger.error {workflow, kind, message}` on the profile's stream and on `/api/events`, and record `trigger` and `input` on the run (shown in `RunDetail` and the runs list).
- TRIGGERS-25: Shutting the server down MUST stop all watchers, schedules and webhook routes before the supervisor stops, and pending settle timers MUST be dropped (TRIGGERS-11 picks them up on the next start).

## Interfaces

Workflow YAML (C-WORKFLOW, additive):
```yaml
name: new-music
input: "{{ vars.inbox }}"          # default run.input for schedule and manual runs
on:
  watch:
    path: "{{ vars.inbox }}"
    settle: 10m                    # default 2m
    poll: 30s                      # optional; omit for native events
  schedule: "0 4 * * *"
  webhook:
    name: lidarr                   # POST /hooks/<profile>/lidarr
    input: trackFiles.0.path       # dotted path into the JSON body
    within: "{{ vars.inbox }}"     # default: on.watch.path
steps:
  - uses: beets.import
    with: { path: "{{ run.input }}", mode: auto }
```

```python
class WatchTrigger(_Strict):
    path: str
    settle: Duration = "2m"
    poll: Duration | None = None

class WebhookTrigger(_Strict):
    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    input: str                      # dotted path
    within: str | None = None

class Workflow(_Strict):
    ...
    input: str | None = None

TriggerKind = Literal["manual", "watch", "schedule", "webhook"]

class TriggerState(BaseModel):     # GET /api/profiles/{profile}/triggers
    workflow: str
    kind: Literal["watch", "schedule", "webhook"]
    state: Literal["armed", "disabled", "error"]
    detail: str | None              # "untrusted", "path missing", ...
    path: str | None                # resolved watch path
    next_fire: datetime | None      # schedule
    last_fired: datetime | None
    last_run_id: str | None
```

App DB (new migration):
```sql
CREATE TABLE trigger_inputs (
  profile TEXT, workflow TEXT, input_path TEXT, fingerprint TEXT,
  run_id TEXT, updated_at TEXT,
  PRIMARY KEY (profile, workflow, input_path)
);
CREATE TABLE webhook_secrets (      -- hashes only; open question 3
  profile TEXT, name TEXT, sha256 TEXT, created_at TEXT,
  PRIMARY KEY (profile, name)
);
ALTER TABLE jobs ADD COLUMN trigger TEXT;   -- TriggerKind
ALTER TABLE jobs ADD COLUMN input TEXT;
```

CLI: `rifferr run <workflow> [--input PATH] [--force]`;
`rifferr webhook new <profile> <name>` (prints the secret once), `rifferr webhook rotate`,
`rifferr webhook list`.

Server modules: `rifferr.triggers` (`TriggerManager`: arming, watcher, scheduler, dedup),
`api/routes/hooks.py`, `api/routes/triggers.py`. Pure parts (cron next-fire, settle
bookkeeping, fingerprint comparison, dotted-path lookup) are in `core` with injected clock
and filesystem so they're tested without real time or files.

## Examples
- `inbox/Artist - Album/` copied over 3 minutes, `settle: 2m`: one run starts about 2 minutes
  after the last file lands, `run.input = /srv/downloads/music/Artist - Album`.
- The server restarts while `Artist - Album` is still in the inbox and its run finished: the
  start-up scan fingerprints it, finds the same fingerprint, emits `trigger.skipped
  {reason: already_run}`.
- A file is added to that folder later: the fingerprint changes, and a new run starts.
- Lidarr posts `{"eventType": "Test"}`: `200 {ignored: "no_input"}`.
- A webhook payload names `/etc`: `422 bad_input`, nothing runs.
- `schedule: "0 4 * * *"`, profile `timezone: Europe/Amsterdam`, server down 03:00–05:00:
  no 04:00 run; next is 04:00 the next day.
- Untrusted `examples/workflows/foobar-style.yaml` copied in: the triggers list shows
  `disabled: untrusted`; `rifferr trust` arms it.

## Extensibility
- Trigger kinds are fixed in M2 (`watch`, `schedule`, `webhook`). A `trigger` plugin entry
  point is a non-goal here (open question 11).
- Callers are configured entirely in workflow YAML (`input`, `within`). Lidarr and slskd get
  documented example workflows, not code.

## Security & safety
- Untrusted workflows never start unattended (TRIGGERS-2).
- Webhooks have their own secrets, constant-time comparison, no secrets in query strings,
  logs or events (TRIGGERS-17). Only their hashes are stored (open question 3).
- Webhook-supplied paths are confined to `within` after resolving symlinks (TRIGGERS-16), so
  a caller with the secret can only start runs on folders under the inbox.
- Body size and content-type limits (TRIGGERS-15). No rate limit in M2 (BACKLOG candidate).
- Remote binding still requires real auth (SECURITY-9); webhooks don't change that.

## Non-goals
- Lanes and per-album parallelism (C-RUN, M2, next spec).
- More than one concurrent run per profile.
- Notifications (S-NOTIFY).
- Cleaning up the inbox after import (S-FILES-CLEANUP).
- Plugin-defined trigger kinds; catch-up of missed schedules.
- Telling Lidarr or a media server anything after a run (architecture §12 integrations).

## Open questions
1. **Dependencies.** Recommend `watchfiles` (MIT) for native events and polling, plus a cron
   library for next-fire times with DST handling: `cronsim` (BSD-3-Clause) or `croniter`
   (MIT). I'd check both projects' current maintenance before choosing. Writing our own
   cron parser is the alternative. OK to add `watchfiles` and which cron library?
2. **Webhook URL.** Architecture says `/hooks/<name>`. Webhook names are only unique within a
   profile, so I recommend `/hooks/<profile>/<name>`. OK?
3. **Webhook secret storage.** Recommend generating a random 32-byte secret with
   `rifferr webhook new` (and a UI button), showing it once, and storing only its SHA-256 in
   the app DB. The plaintext is never stored, which keeps "secrets only in keyring or env".
   The alternative is a profile `secrets:` reference (`keyring:` / `env:`) named by the
   webhook, which works better for Docker setups that inject secrets. Hash store, secret
   reference, or both?
4. **Run input.** Recommend a new top-level `input:` field plus `{{ run.input }}`, so one
   workflow handles watch, webhook, schedule and manual runs. The alternative is having
   triggers override a named var (`on.watch.var: inbox`), which needs no new syntax but
   makes the same var mean "whole inbox" or "one folder" depending on the trigger. Which?
5. **Watch granularity.** Recommend one run per top-level folder of the watch path, with
   loose files in the root ignored (and reported). Some download clients drop single tracks
   or nest `Artist/Album/` two levels deep. Is top-level folder right for your setup, or
   should depth be configurable (`depth: 2`)?
6. **Polling.** Auto-detecting network mounts is unreliable, so I recommend explicit
   `poll:` per watch plus `RIFFERR_FORCE_POLLING=1` for Docker. OK?
7. **Trust and triggers.** Recommend that untrusted workflows never arm (TRIGGERS-2). M1
   lets untrusted workflows run until a `shell` step. Should triggers be stricter than
   that, as proposed, or should a workflow without shell steps arm while untrusted?
8. **Failed runs.** Recommend that a failed run of an unchanged input is not retried by
   triggers (prevents retry loops every night); the user retries from the UI. Or should
   schedule runs retry failures?
9. **Missed schedules.** Recommend no catch-up (watch start-up scan covers the inbox case).
   Or run once at start if a firing was missed?
10. **Host allowlist for webhooks.** A container calling `http://rifferr:8000/hooks/...` sends
    `Host: rifferr`, which the allowlist rejects unless configured. Recommend keeping the
    Host check (documenting how to add the name) since relaxing it is loosening a security
    rule. Keep it?
11. **Plugin trigger kinds.** Recommend deferring a `trigger` entry point to BACKLOG.
12. **Sample payloads.** I don't want to guess Lidarr and slskd payload fields for the example
    workflows. Can you capture a real "on import/download complete" payload from each (paths
    can be redacted) for record/replay fixtures?

## Tasks
- [ ] C-WORKFLOW: `input`, `run.input`, `run.trigger`, new watch/webhook fields, arm-time
      interpolation rules; regenerate schemas.
- [ ] Core: duration parsing, cron next-fire (injected clock, time zone), dotted-path lookup,
      fingerprint, dedup decision; tests TRIGGERS-9, -12..-14, -19..-21.
- [ ] Migration: `trigger_inputs`, `webhook_secrets`, `jobs.trigger`, `jobs.input`.
- [ ] `TriggerManager`: arm/re-arm on workflow file changes and trust; watcher with settle
      and start-up scan; scheduler; shutdown order (TRIGGERS-1..-3, -8..-11, -23, -25).
- [ ] Webhook route and CLI `rifferr webhook new|rotate|list` (TRIGGERS-15..-18).
- [ ] `GET /api/profiles/{profile}/triggers`, trigger events, run `trigger`/`input` in
      `RunDetail` (TRIGGERS-4, -24).
- [ ] `rifferr run --input/--force`; UI run dialog input; "Run again" (TRIGGERS-7, -20).
- [ ] UI: triggers on the workflow page; trigger column in runs list; Playwright flow.
- [ ] `examples/workflows/foobar-style.yaml`: `on.watch` on the inbox, steps use
      `{{ run.input }}`; docs for Lidarr and slskd from recorded payloads.
- [ ] `docs/coverage.md`; mark Implemented.
