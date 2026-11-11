# F-API: HTTP API, SSE event stream and app DB (M0 slice)
Status: Draft
Milestone: M0

## Context
The React UI and the CLI both talk to a FastAPI server bound to localhost
(architecture §8). The server owns the app DB (SQLite, WAL) holding runs,
events, jobs and settings. Progress is pushed over Server-Sent Events; the UI
never polls (AGENTS.md conventions). The OpenAPI document generates the
frontend's TypeScript types (DEV-TOOLING-10).

## Behavior
- API-1: The app DB MUST be a SQLite file in the config root, opened in WAL mode, with schema migrations applied in order at startup and recorded in a `schema_version` table.
- API-2: `GET /api/profiles` MUST list profiles (name, backend, var names) without secret values or references' targets.
- API-3: `GET /api/workflows?profile=` MUST list workflow files for a profile with name, path, validity and trust status; `GET /api/workflows/{name}` MUST return the raw YAML and validation errors.
- API-4: `POST /api/workflows/{name}/validate` MUST return validation errors (C-WORKFLOW) and the static safety report (SECURITY-11).
- API-5: `POST /api/runs` with `{profile, workflow, vars?}` MUST validate, create a run in state `queued`, enqueue a worker job (F-WORKER) and return `201` with the run id.
- API-6: `GET /api/runs` and `GET /api/runs/{id}` MUST return run summaries and detail (state, steps with status, timestamps, workflow hash).
- API-7: `POST /api/runs/{id}/cancel` MUST request cancellation and return `202`.
- API-8: `GET /api/runs/{id}/events` MUST be an SSE stream: it first replays persisted events after `Last-Event-ID` (or from the start), then streams live events; each SSE message's `id` is the event `seq`, `event` is the event type, `data` is the event JSON.
- API-9: `GET /api/events` MUST be an SSE stream of run-level events (`run.started`, `run.finished`) across all runs for the dashboard.
- API-10: Every request and response body MUST be a Pydantic model so the OpenAPI document fully describes it; `GET /openapi.json` MUST be available to the frontend build.
- API-11: Errors MUST use one envelope `{error: {code, message, details?}}` with stable `code` strings.
- API-12: All routes MUST sit behind the F-SECURITY middleware stack.
- API-13: The built frontend MUST be served from `/` by the same server; in `--dev` mode the server proxies to Vite instead.

## Interfaces
```
GET    /healthz
GET    /auth/exchange?token=
GET    /api/profiles
GET    /api/workflows?profile=
GET    /api/workflows/{name}?profile=
POST   /api/workflows/{name}/validate?profile=
POST   /api/runs                       {profile, workflow, vars?}
GET    /api/runs?profile=&limit=
GET    /api/runs/{id}
POST   /api/runs/{id}/cancel
GET    /api/runs/{id}/events           (SSE, honors Last-Event-ID)
GET    /api/events                     (SSE)
```
DB tables (M0): `schema_version`, `runs`, `events`, `jobs`, `trusted_files`, `settings`.

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

## Open questions
- Migrations: hand-written numbered SQL files (stdlib only, as architecture §10 suggests) — confirm no Alembic.
- How long are events kept? Forever, or pruned after N days per profile?
- Should workflow endpoints address workflows by name or by path relative to the profile's workflows dir?

## Tasks
- [ ] app DB + migrations
- [ ] profile and workflow routes
- [ ] run routes
- [ ] SSE with replay
- [ ] error envelope
- [ ] static frontend serving + dev proxy
- [ ] OpenAPI completeness test
