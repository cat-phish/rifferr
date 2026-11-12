"""The app DB (F-WORKER): a SQLite file shared by the server and its workers.

M0 holds only the job table and the event store. F-API extends it (and owns
schema migrations). Access goes through the standard library ``sqlite3`` module.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from rifferr.core.ids import new_ulid
from rifferr.engine.events import Event

JobState = Literal["queued", "running", "succeeded", "failed", "cancelled"]
JobKind = Literal["run"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    profile     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    run_id      TEXT NOT NULL UNIQUE,
    state       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    error       TEXT,
    created_at  TEXT NOT NULL,
    claimed_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs (profile, state, created_at);
CREATE TABLE IF NOT EXISTS events (
    run_id  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    type    TEXT NOT NULL,
    step_id TEXT,
    data    TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class Job(BaseModel):
    id: str
    profile: str
    kind: JobKind
    run_id: str
    state: JobState
    payload: dict[str, JsonValue]
    error: str | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    finished_at: datetime | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AppDB:
    """One connection per process; open it in the thread that uses it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=10000")
        self._conn.executescript(_SCHEMA)
        path.chmod(0o600)

    def close(self) -> None:
        self._conn.close()

    # -- jobs ------------------------------------------------------------------

    def submit(self, profile: str, payload: dict[str, JsonValue], run_id: str | None = None) -> Job:
        job_id, run_id = new_ulid(), run_id or new_ulid()
        self._conn.execute(
            "INSERT INTO jobs (id, profile, kind, run_id, state, payload, created_at)"
            " VALUES (?, ?, 'run', ?, 'queued', ?, ?)",
            (job_id, profile, run_id, json.dumps(payload), _now()),
        )
        job = self.job_for_run(run_id)
        assert job is not None
        return job

    def claim_next(self, profile: str) -> Job | None:
        """Atomically move the oldest queued job of ``profile`` to ``running``."""
        row = self._conn.execute(
            "UPDATE jobs SET state = 'running', claimed_at = ? WHERE id = ("
            "  SELECT id FROM jobs WHERE profile = ? AND state = 'queued'"
            "  ORDER BY created_at, id LIMIT 1"
            ") AND state = 'queued' RETURNING *",
            (_now(), profile),
        ).fetchone()
        return _job(row) if row else None

    def finish(self, run_id: str, state: JobState, error: str | None = None) -> bool:
        """Mark a running job finished. False if it wasn't running (e.g. already failed)."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ?"
            " WHERE run_id = ? AND state = 'running'",
            (state, error, _now(), run_id),
        )
        return cur.rowcount == 1

    def cancel_queued(self, run_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = ?"
            " WHERE run_id = ? AND state = 'queued'",
            (_now(), run_id),
        )
        return cur.rowcount == 1

    def fail_jobs(self, profile: str, states: Iterable[JobState], error: str) -> list[Job]:
        """Fail every job of ``profile`` in ``states``; return them as they were."""
        wanted = list(states)
        marks = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"UPDATE jobs SET state = 'failed', error = ?, finished_at = ?"
            f" WHERE profile = ? AND state IN ({marks}) RETURNING *",
            (error, _now(), profile, *wanted),
        ).fetchall()
        return [_job(r) for r in rows]

    def job_for_run(self, run_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE run_id = ?", (run_id,)).fetchone()
        return _job(row) if row else None

    def jobs(self, profile: str | None = None, state: JobState | None = None) -> list[Job]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", list[Any]()
        if profile is not None:
            sql, args = sql + " AND profile = ?", [*args, profile]
        if state is not None:
            sql, args = sql + " AND state = ?", [*args, state]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [_job(r) for r in rows]

    # -- events ----------------------------------------------------------------

    def append_event(self, event: Event) -> None:
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            last = self.last_seq(event.run_id)
            if event.seq != last + 1:
                raise ValueError(f"run {event.run_id}: expected seq {last + 1}, got {event.seq}")
            self._conn.execute(
                "INSERT INTO events (run_id, seq, ts, type, step_id, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    event.seq,
                    event.ts.isoformat(),
                    event.type,
                    event.step_id,
                    json.dumps(event.data),
                ),
            )

    def last_seq(self, run_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE run_id = ?", (run_id,)
        ).fetchone()
        return int(row[0])

    def events_since(self, run_id: str, seq: int) -> list[Event]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? AND seq > ? ORDER BY seq", (run_id, seq)
        ).fetchall()
        return [
            Event(
                seq=r["seq"],
                ts=datetime.fromisoformat(r["ts"]),
                run_id=r["run_id"],
                type=r["type"],
                step_id=r["step_id"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]


class SqliteEventSink:
    """``EventSink`` backed by the app DB (RUN-7: persisted before fan-out)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    def append(self, event: Event) -> None:
        self.db.append_event(event)

    def since(self, run_id: str, seq: int) -> list[Event]:
        return self.db.events_since(run_id, seq)


def _job(row: sqlite3.Row) -> Job:
    data = dict(row)
    data["payload"] = json.loads(data["payload"])
    return Job.model_validate(data)
