# F-WORKER: Worker process per profile and server ↔ worker channel
Status: Draft
Milestone: M0

## Context
beets' configuration and plugin registry are process-wide singletons, so each
active profile runs in its own worker process; the API server never touches
beets (architecture §8, AGENTS.md layout). The server and workers share a
job/command table in the app DB plus a local IPC channel for events and
(from M2) interactive import decisions.

## Behavior
- WORKER-1: The API server MUST start at most one worker process per profile, on demand when a run for that profile starts, and MUST NOT import beets itself.
- WORKER-2: A worker MUST load exactly one profile and one backend, and refuse commands for any other profile.
- WORKER-3: Run requests MUST be written to the app DB job table first; the worker MUST pick them up from the table so a request survives a worker crash or restart.
- WORKER-4: Events from the worker MUST reach the server over the IPC channel and be persisted by the server (EventSink) before fan-out to SSE subscribers.
- WORKER-5: Cancel requests MUST travel server → worker and be honored per RUN-8.
- WORKER-6: If a worker exits unexpectedly, the server MUST mark its running run `failed` with reason `worker_crashed`, emit `run.finished`, and start a fresh worker for the next request.
- WORKER-7: A worker with no active run for a configurable idle period (default 10 minutes) MUST exit cleanly.
- WORKER-8: On server shutdown, workers MUST receive a stop request, cancel their current run cooperatively, and exit; the server waits up to 10 seconds, then kills them.
- WORKER-9: The IPC channel MUST be reachable only by the same OS user (e.g. a Unix socket with mode 0600 in the user's runtime dir, or an inherited pipe), never a TCP port.

## Interfaces
```python
class WorkerCommand(BaseModel):          # server -> worker
    kind: Literal["start_run", "cancel_run", "stop"]
    run_id: str | None = None

class WorkerMessage(BaseModel):          # worker -> server
    kind: Literal["event", "ready", "heartbeat"]
    event: Event | None = None
```
Job table (app DB): `jobs(id, profile, kind, run_id, state, created_at, claimed_at)`.

## Examples
Start run → server inserts job, spawns `python -m rifferr.worker --profile main`,
worker claims job, streams `run.*` events; server persists them and pushes them
to SSE. `kill -9` on the worker → run shows `failed: worker_crashed`.

## Extensibility
None; internal mechanism. The message models are internal, not published schemas.

## Security & safety
- WORKER-9 keeps the channel off the network.
- Workers inherit no secrets in their environment beyond what the profile resolves at point of use.

## Non-goals
- Interactive import decision round-trips (F-IMPORT-UI, M2).
- Multiple concurrent runs per profile (F-TRIGGERS, M2).

## Open questions
- IPC mechanism: `multiprocessing` pipes vs a Unix domain socket (architecture §12, BACKLOG B-009). A socket lets a worker survive a server restart; pipes are simpler. Which?
- Is idle exit (WORKER-7) wanted, or should workers stay up for fast previews in M1?
- Heartbeat interval and timeout for detecting a hung (not crashed) worker?

## Tasks
- [ ] worker entrypoint
- [ ] job table + claim logic
- [ ] IPC channel
- [ ] crash detection and recovery
- [ ] idle exit + shutdown
