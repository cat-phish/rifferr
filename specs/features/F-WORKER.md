# F-WORKER: Worker process per profile and server ↔ worker channel
Status: Implemented
Milestone: M0

## Context
beets' configuration and plugin registry are process-wide singletons, so each
active profile runs in its own worker process; the API server never touches
beets (architecture §8, AGENTS.md layout). The server and workers share a
job/command table in the app DB plus a local IPC channel for events and
(from M2) interactive import decisions.

The app DB arrives here in minimal form (jobs and events, SQLite via the Python
standard library); F-API extends it.


M1 additions (WORKER-11) are specified in [S-TAGSCRIPT](../steps/S-TAGSCRIPT.md) (approved 2026-11-11) and implemented with it.
## Behavior
- WORKER-1: The API server MUST start at most one worker process per profile, on demand when a run for that profile starts, as `<server's own Python> -m rifferr.worker --profile <name>`, and MUST NOT import beets itself.
- WORKER-2: A worker MUST load exactly one profile and one backend, and refuse commands for any other profile.
- WORKER-3: Run requests MUST be written to the app DB job table first; the worker MUST pick them up from the table so a request survives a worker crash or restart. The app DB MUST be a SQLite file in Rifferr's data directory, accessed only through the standard library `sqlite3` module.
- WORKER-4: Events from the worker MUST reach the server over the IPC channel and be persisted by the server (EventSink) before fan-out to SSE subscribers.
- WORKER-5: Cancel requests MUST travel server → worker and be honored per RUN-8.
- WORKER-6: If a worker exits unexpectedly, the server MUST mark its running run `failed` with reason `worker_crashed`, emit `run.finished`, and start a fresh worker for the next request.
- WORKER-7: A worker with no active run for the profile's `worker.idle_timeout` (seconds, default 600) MUST exit cleanly. `idle_timeout: null` MUST keep the worker running until server shutdown.
- WORKER-8: On server shutdown, workers MUST receive a stop request, cancel their current run cooperatively, and exit; the server waits up to 10 seconds, then sends SIGKILL to the worker's process group, so commands started by its steps die with it.
- WORKER-9: The IPC channel MUST be the worker's inherited stdin (server → worker) and stdout (worker → server), carrying one JSON message per line. Nothing else may be written to the worker's stdout; its stderr goes to the server log. No socket or TCP port is opened.
- WORKER-10: A worker MUST send a heartbeat every 5 seconds from a thread independent of step execution. The server MUST treat a worker silent for 30 seconds as hung, kill its process group, and handle it as in WORKER-6 with reason `worker_hung`.
- WORKER-11: The server MAY start a second worker per profile in read-only mode (`-m rifferr.worker --profile P --read-only`) for preview and query jobs. It opens the backend read-only (BACKEND-13), accepts no run, apply or undo jobs, takes requests over the same IPC channel (WORKER-9), and follows the same idle timeout, heartbeat and shutdown rules. Its jobs are not stored in the job table: they are short-lived and lost on a crash, and the request fails with `503 preview_unavailable` (added for S-TAGSCRIPT).

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

Profile setting (extends C-PROFILE's `profile.yaml`):
```yaml
worker:
  idle_timeout: 600        # seconds; null = stay up until server shutdown
```

As implemented:
- `rifferr.appdb`: `AppDB` (SQLite, WAL, file mode 0600) with `jobs` and `events` tables, and
  `SqliteEventSink`. Jobs also store `payload` (workflow **text**, source name, run vars,
  `trusted`), `error` and `finished_at`. The worker runs the stored text, so it runs exactly
  what the server checked. Job states: queued, running, succeeded, failed, cancelled.
- App DB location: `RIFFERR_DATA_DIR`, else `RIFFERR_CONFIG_DIR` (Docker `/config`), else
  `$XDG_DATA_HOME/rifferr`, else `~/.local/share/rifferr`; file `rifferr.db`.
- `rifferr.worker.protocol`: `WorkerCommand` carries `profile`; `WorkerMessage` adds
  `kind: "error"` with `message`. `start_run` only wakes the worker; it claims jobs itself.
- `rifferr.worker.process`: `Worker` (testable in-process) and `main()` for
  `python -m rifferr.worker --profile <name> [--db <path>]`. Startup failures (missing
  profile, backend not installed) are reported as an `error` message and exit status 2.
- `rifferr.worker.supervisor.WorkerSupervisor(db, sink, command=, hung_after=30,
  stop_grace=10)`: `submit_run`, `cancel`, `subscribe`, `shutdown`. Workers run in their own
  process group.
- `run.finished` written by the server carries `reason`: `worker_crashed`, `worker_hung`,
  `server_shutdown` (killed after the stop grace), or `worker_failed_to_start`. A worker that
  fails before `ready` fails the profile's queued jobs too, instead of restarting in a loop.
- A stop leaves queued jobs queued for the next worker.
- Since F-SECURITY, `submit_run` decides `trusted` itself from the app DB trust store.

## Examples
Start run → server inserts job, spawns `python -m rifferr.worker --profile main`,
worker claims job, streams `run.*` events; server persists them and pushes them
to SSE. `kill -9` on the worker → run shows `failed: worker_crashed`.

## Extensibility
None; internal mechanism. The message models are internal, not published schemas.

## Security & safety
- WORKER-9 keeps the channel off the network: only the parent process holds the pipes.
- Workers inherit the server's environment so `env:` secrets can be resolved at point of use (PROFILE-5); commands started by steps see those values only when the step lists them (SHELL-4).

## Non-goals
- Interactive import decision round-trips (F-IMPORT-UI, M2).
- Multiple concurrent runs per profile (F-TRIGGERS, M2).
- Workers surviving a server restart (would need a socket instead of pipes).
- Notifications about worker crashes, hangs or idle exits (B-019, with S-NOTIFY in M2).
- App DB schema migrations beyond creating the jobs and events tables (F-API).

## Open questions
- ~~IPC mechanism (B-009)?~~ Resolved 2026-11-11 (author): inherited pipes, JSON lines (WORKER-9).
- ~~Idle exit?~~ Resolved 2026-11-11 (author): a per-profile user option, default 600 s, `null` disables it (WORKER-7). Notifications idea → B-019.
- ~~Heartbeat?~~ Resolved 2026-11-11 (author): every 5 s, hung after 30 s (WORKER-10).

## Tasks
- [x] worker entrypoint
- [x] job table + claim logic
- [x] IPC channel
- [x] crash and hang detection and recovery
- [x] `worker.idle_timeout` profile setting (C-PROFILE extension)
- [x] idle exit + shutdown
