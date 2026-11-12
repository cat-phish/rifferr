# F-API: HTTP API, SSE event stream and app DB (M0 slice)
Status: Draft
Milestone: M0

## Context
The React UI and the CLI both talk to a FastAPI server bound to localhost
(architecture §8). The server owns the app DB (SQLite, WAL; `rifferr.appdb`,
introduced by F-WORKER) holding jobs, events and trusted files. In M0 the run
record is the job row (one job per run); runs are submitted through
`WorkerSupervisor`, which also decides trust (SECURITY-10). Progress is pushed over Server-Sent Events; the UI
never polls (AGENTS.md conventions). The OpenAPI document generates the
frontend's TypeScript types (DEV-TOOLING-10).

## Behavior
- API-1: The app DB MUST be the SQLite file at `profile_store.app_db_path()` (F-WORKER), opened in WAL mode. Schema changes MUST be numbered, hand-written SQL files (`rifferr/migrations/NNN_name.sql`, stdlib `sqlite3` only), applied in order at startup and recorded in a `schema_version` table. `001_init.sql` MUST create the tables F-WORKER and F-SECURITY already use, so existing DB files upgrade without loss.
- API-2: `GET /api/profiles` MUST list profiles (name, backend, var names) without secret values or references' targets.
- API-3: `GET /api/workflows?profile=` MUST list workflow files for a profile with name, path, validity, content hash and trust status; `GET /api/workflows/{name}?profile=` MUST return the raw YAML, its hash and validation errors. A workflow's `name` is its file stem in the profile's `workflows/` folder. A name containing `/`, a backslash or `..`, or a stem shared by a `.yaml` and a `.yml` file, MUST be an error (`invalid_workflow_name`, `ambiguous_workflow_name`).
- API-4: `POST /api/workflows/{name}/validate` MUST return validation errors (C-WORKFLOW) and the static safety report (SECURITY-11).
- API-5: `POST /api/runs` with `{profile, workflow, vars?}` MUST read the workflow file as bytes (F-SECURITY), validate it against the step registry and the profile's vars (without backend capabilities: the server never loads beets; the worker re-validates in full), submit it through `WorkerSupervisor.submit_run` (which stores the text and decides trust) and return `201` with the run id. Validation failures MUST return `422 validation_failed` with the errors and create no run.
- API-6: `GET /api/runs?profile=&limit=` and `GET /api/runs/{id}` MUST return run summaries and detail (state, trusted, steps with status, timestamps, workflow name and hash). Step statuses MUST be derived from the run's persisted events over the step list of the stored workflow text, so a queued run shows its steps as `pending`.
- API-7: `POST /api/runs/{id}/cancel` MUST request cancellation and return `202`.
- API-8: `GET /api/runs/{id}/events` MUST be an SSE stream: it first replays persisted events after `Last-Event-ID` (or from the start), then streams live events; each SSE message's `id` is the event `seq`, `event` is the event type, `data` is the event JSON. The server MUST subscribe before replaying and drop duplicates by `seq`, so no event is lost or repeated. The stream MUST close after sending `run.finished` (immediately for a finished run); clients close their EventSource when they receive it. Idle streams MUST send a `: keepalive` comment every 15 seconds.
- API-9: `GET /api/events` MUST be an SSE stream of run-level events (`run.started`, `run.finished`) across all runs for the dashboard.
- API-10: Every request and response body MUST be a Pydantic model so the OpenAPI document fully describes it; `GET /openapi.json` MUST be available to the frontend build.
- API-11: Errors MUST use one envelope `{error: {code, message, details?}}` with stable `code` strings, including the rejections of the F-SECURITY middleware. M0 codes: `host_not_allowed`, `cross_origin`, `unsupported_media_type`, `unauthenticated`, `invalid_token`, `not_found`, `invalid_workflow_name`, `ambiguous_workflow_name`, `validation_failed`, `hash_mismatch`, `bad_request`.
- API-12: All routes MUST sit behind the F-SECURITY middleware stack.
- API-13: The built frontend MUST be served from `/` by the same server, from a configurable directory (default `frontend/dist`); without a build, `/` MUST show a short page saying how to build it. In `--dev` mode the Vite dev server serves the UI and proxies `/api`, `/auth` and `/healthz` to the backend, and the backend MUST additionally accept the Vite origin (`http://localhost:<vite port>`, `http://127.0.0.1:<vite port>`) in the Origin check. Only with `--dev`; Host, JSON and auth checks are unchanged (author approved this dev-only exception 2026-11-11).
- API-14: `POST /api/workflows/{name}/trust?profile=` with `{sha256}` MUST trust that exact content (SECURITY-12, the UI's confirm after showing the safety report). If the file's current hash differs, it MUST return `409 hash_mismatch` and trust nothing.
- API-15: `create_app(settings)` MUST open the app DB and apply migrations, create the `WorkerSupervisor`, generate the local token and write the token file at startup, and on shutdown stop the workers (WORKER-8) and remove the token file.

## Interfaces
```
GET    /healthz
GET    /auth/exchange?token=
GET    /api/profiles
GET    /api/workflows?profile=
GET    /api/workflows/{name}?profile=
POST   /api/workflows/{name}/validate?profile=
POST   /api/workflows/{name}/trust?profile=     {sha256}
POST   /api/runs                       {profile, workflow, vars?}
GET    /api/runs?profile=&limit=
GET    /api/runs/{id}
POST   /api/runs/{id}/cancel
GET    /api/runs/{id}/events           (SSE, honors Last-Event-ID)
GET    /api/events                     (SSE)
```
DB tables (M0): `schema_version`, `jobs` (also the run record: adds `workflow`,
`workflow_hash`, `started_at`), `events`, `trusted_files`. A `settings` table waits until
there is a setting to store.

## Examples
SSE message:
```
id: 7
event: run.step.log
data: {"seq":7,"ts":"2026-11-11T12:00:03Z","run_id":"…","type":"run.step.log","step_id":"list-files","data":{"stream":"stdout","line":"total 12"}}
```

## Extensibility
Step types cannot add routes in M0. Event `data` shapes are owned by the emitting step and documented in its spec.

## Security & safety
- No secrets in any response (PROFILE-5).
- SSE endpoints are GETs but still require auth (SECURITY-7).

## Non-goals
- Review queue, journal, tag grid routes (M1). Webhooks (M2).
- Workflow editing/saving routes (later; round-trip save exists in C-WORKFLOW).
- Event pruning (B-021); M0 keeps events forever.
- `rifferr serve` and opening the browser (F-CLI).

## Open questions
- ~~Migrations?~~ Resolved 2026-11-11 (author): numbered SQL files, stdlib only (API-1).
- ~~Event retention?~~ Resolved 2026-11-11 (author): forever in M0; pruning → B-021.
- ~~Workflow addressing?~~ Resolved 2026-11-11 (author): file stem (API-3).
- ~~Dev proxy direction?~~ Resolved 2026-11-11 (author): Vite proxies to the backend, with a dev-only allowed origin (API-13).

## Tasks
- [ ] app DB + migrations (and security middleware on the error envelope)
- [ ] profile and workflow routes
- [ ] run routes
- [ ] SSE with replay
- [ ] error envelope
- [ ] static frontend serving + dev proxy
- [ ] trust route
- [ ] OpenAPI completeness test (and real `openapi.json` export, B-016)
