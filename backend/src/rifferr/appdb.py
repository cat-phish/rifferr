"""The app DB (F-WORKER): a SQLite file shared by the server and its workers.

M0 holds only the job table and the event store. F-API extends it (and owns
schema migrations). Access goes through the standard library ``sqlite3`` module.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, JsonValue

from rifferr.core.changeset import ApplyResult, ChangeSet, ChangeSetState, JournalEntry
from rifferr.core.ids import new_ulid
from rifferr.core.imports import ImportDecision, ImportDecisionState
from rifferr.core.review import GateState, ReviewGate
from rifferr.core.triggers import DedupRecord
from rifferr.engine.changesets import StoredChangeSet
from rifferr.engine.events import Event
from rifferr.engine.journal import JournalError, completed, failed, recovered, undone
from rifferr.engine.lanes import FINAL_LANE_STATES, Hold, Lane

JobState = Literal["queued", "running", "waiting_review", "succeeded", "failed", "cancelled"]
#: ``run``: the run record itself (API-6). ``review_decision``: apply or reject a
#: ChangeSet (CHANGESET-13). ``undo``: undo a journal entry or a run (C-JOURNAL).
#: ``review_reject``: undo a run rejected at a review gate, then end it (REVIEW-8).
#: ``manual_apply``: apply manual ChangeSets from the tag grid (TAG-GRID-8).
#: ``repropose``: propose a conflicted ChangeSet again on current values (REVIEW-QUEUE-5).
JobKind = Literal[
    "run",
    "review_decision",
    "undo",
    "review_reject",
    "manual_apply",
    "repropose",
    "import_decision",
    "import_lookup",
]

#: ChangeSet states that still hold a suspended run (CHANGESET-7).
HOLDING_CHANGESETS = ("proposed", "conflicted")

_MIGRATION_FILE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


def migrations() -> list[tuple[int, str, str]]:
    """``(version, name, sql)`` for every migration shipped in ``rifferr.migrations``."""
    found: list[tuple[int, str, str]] = []
    for entry in importlib.resources.files("rifferr.migrations").iterdir():
        m = _MIGRATION_FILE.match(entry.name)
        if m:
            found.append((int(m[1]), m[2], entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if versions != list(range(1, len(found) + 1)):
        raise RuntimeError(
            f"app DB migrations must be numbered 001, 002, ... without gaps: {versions}"
        )
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()
    return int(row[0])


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order (API-1). Safe when several processes start at once:
    each migration records its version first, so a second process applying the same one
    fails on the primary key and rolls back. Returns the resulting version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    for version, name, sql in migrations():
        if version <= schema_version(conn):
            continue
        script = (
            "BEGIN IMMEDIATE;\n"
            "INSERT INTO schema_version (version, name, applied_at)"
            f" VALUES ({version}, '{name}', '{_now()}');\n"
            f"{sql}\nCOMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.IntegrityError:
            conn.rollback()  # applied by another process meanwhile
    return schema_version(conn)


class Job(BaseModel):
    id: str
    profile: str
    kind: JobKind
    run_id: str | None
    state: JobState
    payload: dict[str, JsonValue]
    error: str | None = None
    created_at: datetime
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    workflow: str | None = None
    workflow_hash: str | None = None
    trigger: str | None = None
    """What started a run job: manual, watch, schedule or webhook (F-TRIGGERS)."""
    input: str | None = None
    """The run's ``{{ run.input }}`` as given by its trigger (F-TRIGGERS)."""
    result: JsonValue = None
    """What a finished non-run job did (e.g. an undo's report)."""

    @property
    def run(self) -> str:
        """The run id of a ``run`` job (always set for that kind)."""
        if self.run_id is None:
            raise ValueError(f"job {self.id} ({self.kind}) has no run")
        return self.run_id


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
        migrate(self._conn)
        path.chmod(0o600)

    def close(self) -> None:
        self._conn.close()

    # -- jobs ------------------------------------------------------------------

    def submit(
        self,
        profile: str,
        payload: dict[str, JsonValue],
        run_id: str | None = None,
        *,
        workflow: str | None = None,
        workflow_hash: str | None = None,
        trigger: str = "manual",
        input: str | None = None,
    ) -> Job:
        job_id, run_id = new_ulid(), run_id or new_ulid()
        self._conn.execute(
            "INSERT INTO jobs (id, profile, kind, run_id, state, payload, created_at,"
            " workflow, workflow_hash, trigger, input)"
            " VALUES (?, ?, 'run', ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (
                job_id,
                profile,
                run_id,
                json.dumps(payload),
                _now(),
                workflow,
                workflow_hash,
                trigger,
                input,
            ),
        )
        job = self.job_for_run(run_id)
        assert job is not None
        return job

    def submit_job(
        self, profile: str, kind: JobKind, payload: dict[str, JsonValue], run_id: str | None
    ) -> Job:
        """Queue a job that isn't a run (e.g. a review decision about one of its ChangeSets)."""
        if kind == "run":
            raise ValueError("use submit() for runs")
        job_id = new_ulid()
        row = self._conn.execute(
            "INSERT INTO jobs (id, profile, kind, run_id, state, payload, created_at)"
            " VALUES (?, ?, ?, ?, 'queued', ?, ?) RETURNING *",
            (job_id, profile, kind, run_id, json.dumps(payload), _now()),
        ).fetchone()
        return _job(row)

    def job(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

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
            " WHERE run_id = ? AND kind = 'run' AND state = 'running'",
            (state, error, _now(), run_id),
        )
        return cur.rowcount == 1

    def finish_job(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        result: JsonValue = None,
    ) -> bool:
        """Like ``finish``, by job id (for jobs that aren't runs), with an optional result."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = ?, error = ?, finished_at = ?, result = ?"
            " WHERE id = ? AND state = 'running'",
            (state, error, _now(), None if result is None else json.dumps(result), job_id),
        )
        return cur.rowcount == 1

    def suspend(
        self, run_id: str, after: str, holds: list[str], gates: list[str] | None = None
    ) -> bool:
        """running -> waiting_review until ``holds`` (ChangeSet ids) and ``gates`` (review
        gate ids) are resolved; the run then resumes after step ``after`` (RUN-14)."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'waiting_review', payload = json_set(payload,"
            " '$.resume_after', ?, '$.holds', json(?), '$.gates', json(?))"
            " WHERE run_id = ? AND kind = 'run' AND state = 'running'",
            (after, json.dumps(holds), json.dumps(gates or []), run_id),
        )
        return cur.rowcount == 1

    def requeue_if_released(self, run_id: str) -> bool:
        """Queue a waiting run once a lane of it is released (LANES-8): all of that lane's
        holds are resolved. A run without lanes (suspended before C-LANES) is released
        when none of its payload holds is pending (RUN-14).

        One transaction, so two decisions finishing at once can't both see the other
        still pending and leave the run waiting. True if the run was queued. A run that
        is already queued or running needs nothing: its job picks the lane up (LANES-8).
        """
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT payload FROM jobs WHERE run_id = ? AND kind = 'run'"
                " AND state = 'waiting_review'",
                (run_id,),
            ).fetchone()
            if row is None:
                return False
            waiting = self._conn.execute(
                "SELECT holds FROM run_lanes WHERE run_id = ? AND state = 'waiting'",
                (run_id,),
            ).fetchall()
            has_lanes = (
                self._conn.execute(
                    "SELECT 1 FROM run_lanes WHERE run_id = ? LIMIT 1", (run_id,)
                ).fetchone()
                is not None
            )
            if has_lanes:
                released = any(
                    self._released(run_id, [_hold(h) for h in json.loads(w["holds"])])
                    for w in waiting
                )
            else:
                payload = json.loads(row["payload"])
                released = self._released(
                    run_id,
                    [Hold("changeset", str(h)) for h in payload.get("holds") or []]
                    + [Hold("gate", str(g)) for g in payload.get("gates") or []],
                )
            if not released:
                return False
            cur = self._conn.execute(
                "UPDATE jobs SET state = 'queued', claimed_at = NULL"
                " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
                (run_id,),
            )
            return cur.rowcount == 1

    def _released(self, run_id: str, holds: list[Hold]) -> bool:
        changesets = [h.ref for h in holds if h.kind == "changeset"]
        gates = [h.ref for h in holds if h.kind == "gate"]
        others = [h.ref for h in holds if h.kind not in ("changeset", "gate")]
        if changesets:
            marks = ",".join("?" for _ in changesets)
            held = self._conn.execute(
                f"SELECT 1 FROM changesets WHERE id IN ({marks})"
                f" AND state IN {HOLDING_CHANGESETS} LIMIT 1",
                changesets,
            ).fetchone()
            if held is not None:
                return False
        if gates:
            marks = ",".join("?" for _ in gates)
            open_gate = self._conn.execute(
                f"SELECT 1 FROM review_gates WHERE id IN ({marks})"
                " AND state != 'continued' LIMIT 1",
                gates,
            ).fetchone()
            if open_gate is not None:
                return False
        for ref in others:
            done = self._conn.execute(
                "SELECT 1 FROM resolved_holds WHERE run_id = ? AND ref = ?", (run_id, ref)
            ).fetchone()
            if done is None:
                return False
        return True

    def requeue(self, run_id: str) -> bool:
        """waiting_review -> queued, so a worker resumes the run (RUN-14)."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'queued', claimed_at = NULL"
            " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
            (run_id,),
        )
        return cur.rowcount == 1

    def finish_waiting(self, run_id: str, state: JobState, error: str | None = None) -> bool:
        """waiting_review -> a final state, once no lane waits any more (LANES-7, -9)."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = ?, finished_at = ?, error = ?"
            " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
            (state, _now(), error, run_id),
        )
        return cur.rowcount == 1

    def cancel_waiting(self, run_id: str, error: str | None = None) -> bool:
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = ?, error = ?"
            " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
            (_now(), error, run_id),
        )
        return cur.rowcount == 1

    def cancel_queued(self, run_id: str) -> bool:
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'cancelled', finished_at = ?"
            " WHERE run_id = ? AND kind = 'run' AND state = 'queued'",
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

    def mark_started(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE jobs SET started_at = COALESCE(started_at, ?)"
            " WHERE run_id = ? AND kind = 'run'",
            (_now(), run_id),
        )

    def list_runs(self, profile: str | None = None, limit: int = 50) -> list[Job]:
        """Newest first."""
        sql, args = "SELECT * FROM jobs WHERE kind = 'run'", list[Any]()
        if profile is not None:
            sql, args = sql + " AND profile = ?", [profile]
        rows = self._conn.execute(
            sql + " ORDER BY created_at DESC, id DESC LIMIT ?", [*args, limit]
        ).fetchall()
        return [_job(r) for r in rows]

    def job_for_run(self, run_id: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE run_id = ? AND kind = 'run'", (run_id,)
        ).fetchone()
        return _job(row) if row else None

    def jobs(self, profile: str | None = None, state: JobState | None = None) -> list[Job]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", list[Any]()
        if profile is not None:
            sql, args = sql + " AND profile = ?", [*args, profile]
        if state is not None:
            sql, args = sql + " AND state = ?", [*args, state]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [_job(r) for r in rows]

    # -- trust (F-SECURITY) ----------------------------------------------------

    def trust(self, sha256: str, path_hint: str, trusted_by: str) -> None:
        """Record an explicit user decision to trust this exact content (SECURITY-12)."""
        self._conn.execute(
            "INSERT INTO trusted_files (sha256, path_hint, trusted_at, trusted_by)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (sha256) DO UPDATE SET"
            " path_hint = excluded.path_hint, trusted_at = excluded.trusted_at,"
            " trusted_by = excluded.trusted_by",
            (sha256, path_hint, _now(), trusted_by),
        )

    def is_trusted(self, sha256: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trusted_files WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return row is not None

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

    def append_next(
        self, stream: str, type_: str, data: dict[str, JsonValue], step_id: str | None = None
    ) -> Event:
        """Append an event with the next seq of ``stream``, chosen inside the write
        transaction, so the server can share a stream with a worker (F-TRIGGERS)."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            event = Event(
                seq=self.last_seq(stream) + 1,
                ts=datetime.now(UTC),
                run_id=stream,
                type=type_,
                step_id=step_id,
                data=data,
            )
            self._conn.execute(
                "INSERT INTO events (run_id, seq, ts, type, step_id, data)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (stream, event.seq, event.ts.isoformat(), type_, step_id, json.dumps(data)),
            )
        return event

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


class AppChangeSets:
    """``ChangeSetStore`` backed by the ``changesets`` tables (CHANGESET-5)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def add(self, changeset: ChangeSet) -> None:
        self._conn.execute(
            "INSERT INTO changesets (id, profile, run_id, step_id, state, body, created_at)"
            " VALUES (?, ?, ?, ?, 'proposed', ?, ?)",
            (
                changeset.id,
                changeset.profile,
                changeset.run_id,
                changeset.step_id,
                changeset.model_dump_json(),
                changeset.created_at.isoformat(),
            ),
        )

    def get(self, changeset_id: str) -> StoredChangeSet | None:
        row = self._conn.execute(
            "SELECT * FROM changesets WHERE id = ?", (changeset_id,)
        ).fetchone()
        return self._stored(row) if row else None

    def set_state(
        self,
        changeset_id: str,
        state: ChangeSetState,
        *,
        resolved_by: str | None = None,
        decisions: dict[int, bool] | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            cur = self._conn.execute(
                "UPDATE changesets SET state = ?, resolved_at = ?,"
                " resolved_by = COALESCE(?, resolved_by) WHERE id = ?",
                (state, None if state == "proposed" else _now(), resolved_by, changeset_id),
            )
            if cur.rowcount != 1:
                raise KeyError(changeset_id)
            if decisions is not None:
                self._conn.execute(
                    "DELETE FROM changeset_decisions WHERE changeset_id = ?", (changeset_id,)
                )
                self._conn.executemany(
                    "INSERT INTO changeset_decisions (changeset_id, idx, accepted)"
                    " VALUES (?, ?, ?)",
                    [(changeset_id, i, int(ok)) for i, ok in sorted(decisions.items())],
                )

    def replace(self, old_id: str, new: ChangeSet | None, *, resolved_by: str) -> bool:
        """Reject ``old_id`` and put ``new`` (if any) in its place among the holds of its
        waiting run, in one transaction (REVIEW-QUEUE-5). False, with nothing changed,
        if ``old_id`` no longer waits for a decision."""
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT run_id FROM changesets WHERE id = ? AND state = 'conflicted'", (old_id,)
            ).fetchone()
            if row is None:
                return False
            if new is not None:
                self._conn.execute(
                    "INSERT INTO changesets (id, profile, run_id, step_id, state, body,"
                    " created_at) VALUES (?, ?, ?, ?, 'proposed', ?, ?)",
                    (
                        new.id,
                        new.profile,
                        new.run_id,
                        new.step_id,
                        new.model_dump_json(),
                        new.created_at.isoformat(),
                    ),
                )
            self._conn.execute(
                "UPDATE changesets SET state = 'rejected', resolved_at = ?, resolved_by = ?"
                " WHERE id = ?",
                (_now(), resolved_by, old_id),
            )
            run_id = row["run_id"]
            job = self._conn.execute(
                "SELECT id, payload FROM jobs WHERE run_id = ? AND kind = 'run'"
                " AND state = 'waiting_review'",
                (run_id,),
            ).fetchone()
            if job is not None and new is not None:
                payload = json.loads(job["payload"])
                holds = [str(h) for h in payload.get("holds") or []]
                payload["holds"] = [new.id if h == old_id else h for h in holds]
                self._conn.execute(
                    "UPDATE jobs SET payload = ? WHERE id = ?", (json.dumps(payload), job["id"])
                )
            if new is not None:  # the lane that waited for the old one waits for the new
                lanes = self._conn.execute(
                    "SELECT key, holds FROM run_lanes WHERE run_id = ? AND state = 'waiting'",
                    (run_id,),
                ).fetchall()
                for lane in lanes:
                    holds = json.loads(lane["holds"])
                    if any(h.get("ref") == old_id for h in holds):
                        swapped = [
                            {**h, "ref": new.id} if h.get("ref") == old_id else h for h in holds
                        ]
                        self._conn.execute(
                            "UPDATE run_lanes SET holds = ?, updated_at = ?"
                            " WHERE run_id = ? AND key = ?",
                            (json.dumps(swapped), _now(), run_id, lane["key"]),
                        )
            return True

    def set_note(self, changeset_id: str, note: str | None) -> None:
        """The reviewer's note on the decision (REVIEW-QUEUE-9)."""
        self._conn.execute("UPDATE changesets SET note = ? WHERE id = ?", (note, changeset_id))

    def note(self, changeset_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT note FROM changesets WHERE id = ?", (changeset_id,)
        ).fetchone()
        return str(row["note"]) if row and row["note"] is not None else None

    def waiting(
        self, profile: str | None = None, run_id: str | None = None
    ) -> list[StoredChangeSet]:
        """ChangeSets of runs that wait for a decision (proposed or conflicted), oldest
        first (REVIEW-QUEUE-1)."""
        sql = f"SELECT * FROM changesets WHERE run_id IS NOT NULL AND state IN {HOLDING_CHANGESETS}"
        args: list[Any] = []
        if profile is not None:
            sql, args = sql + " AND profile = ?", [*args, profile]
        if run_id is not None:
            sql, args = sql + " AND run_id = ?", [*args, run_id]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [self._stored(r) for r in rows]

    def manual(self, profile: str, source_name: str, limit: int) -> list[StoredChangeSet]:
        """The profile's ChangeSets without a run from source ``manual`` / ``source_name``,
        newest first (TAG-GRID-11)."""
        rows = self._conn.execute(
            "SELECT * FROM changesets WHERE profile = ? AND run_id IS NULL"
            " AND json_extract(body, '$.source.kind') = 'manual'"
            " AND json_extract(body, '$.source.name') = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (profile, source_name, limit),
        ).fetchall()
        return [self._stored(r) for r in rows]

    def for_run(self, run_id: str, state: ChangeSetState | None = None) -> list[StoredChangeSet]:
        sql, args = "SELECT * FROM changesets WHERE run_id = ?", [run_id]
        if state is not None:
            sql, args = sql + " AND state = ?", [*args, state]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [self._stored(r) for r in rows]

    def _stored(self, row: sqlite3.Row) -> StoredChangeSet:
        decisions = {
            int(r["idx"]): bool(r["accepted"])
            for r in self._conn.execute(
                "SELECT idx, accepted FROM changeset_decisions WHERE changeset_id = ?",
                (row["id"],),
            )
        }
        return StoredChangeSet(
            ChangeSet.model_validate_json(row["body"]),
            row["state"],
            row["resolved_by"],
            decisions,
        )


class GateResolvedError(ValueError):
    """The gate was already decided or cancelled (REVIEW-6: ``409 already_resolved``)."""

    def __init__(self, gate_id: str, state: str) -> None:
        self.gate_id, self.state = gate_id, state
        super().__init__(f"review gate {gate_id} is already {state}")


class AppReviewGates:
    """``GateStore`` backed by the ``review_gates`` table (S-REVIEW)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def add(self, gate: ReviewGate) -> None:
        self._conn.execute(
            "INSERT INTO review_gates (id, profile, run_id, step_id, state, body, created_at,"
            " deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                gate.id,
                gate.profile,
                gate.run_id,
                gate.step_id,
                gate.state,
                gate.model_dump_json(),
                gate.created_at.isoformat(),
                _utc(gate.deadline) if gate.deadline is not None else None,
            ),
        )

    def get(self, gate_id: str) -> ReviewGate | None:
        row = self._conn.execute("SELECT * FROM review_gates WHERE id = ?", (gate_id,)).fetchone()
        return self._gate(row) if row else None

    def for_run(self, run_id: str, state: GateState | None = None) -> list[ReviewGate]:
        sql, args = "SELECT * FROM review_gates WHERE run_id = ?", [run_id]
        if state is not None:
            sql, args = sql + " AND state = ?", [*args, state]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [self._gate(r) for r in rows]

    def pending(self, profile: str | None = None, run_id: str | None = None) -> list[ReviewGate]:
        """Pending gates, oldest first (REVIEW-QUEUE-1)."""
        sql, args = "SELECT * FROM review_gates WHERE state = 'pending'", list[Any]()
        if profile is not None:
            sql, args = sql + " AND profile = ?", [*args, profile]
        if run_id is not None:
            sql, args = sql + " AND run_id = ?", [*args, run_id]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [self._gate(r) for r in rows]

    def overdue(self, now: datetime) -> list[ReviewGate]:
        """Pending gates whose deadline has passed (REVIEW-9), oldest deadline first."""
        rows = self._conn.execute(
            "SELECT * FROM review_gates WHERE state = 'pending' AND deadline IS NOT NULL"
            " AND deadline <= ? ORDER BY deadline, id",
            (_utc(now),),
        ).fetchall()
        return [self._gate(r) for r in rows]

    def next_deadline(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT MIN(deadline) FROM review_gates WHERE state = 'pending'"
            " AND deadline IS NOT NULL"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def decide(
        self, gate_id: str, state: GateState, *, by: str | None, note: str | None = None
    ) -> ReviewGate:
        """pending -> ``state``, exactly once (REVIEW-6). Raises ``KeyError`` for an
        unknown gate and ``GateResolvedError`` if it was already decided."""
        cur = self._conn.execute(
            "UPDATE review_gates SET state = ?, decided_at = ?, decided_by = ?, note = ?"
            " WHERE id = ? AND state = 'pending'",
            (state, _now(), by, note, gate_id),
        )
        gate = self.get(gate_id)
        if gate is None:
            raise KeyError(gate_id)
        if cur.rowcount != 1:
            raise GateResolvedError(gate_id, gate.state)
        return gate

    def cancel(self, gate_id: str) -> bool:
        """Cancel one pending gate (its albums' lanes ended another way)."""
        cur = self._conn.execute(
            "UPDATE review_gates SET state = 'cancelled', decided_at = ?"
            " WHERE id = ? AND state = 'pending'",
            (_now(), gate_id),
        )
        return cur.rowcount == 1

    def cancel_for_run(self, run_id: str) -> list[str]:
        """Cancel the run's pending gates (REVIEW-10). Returns their ids."""
        rows = self._conn.execute(
            "UPDATE review_gates SET state = 'cancelled', decided_at = ?"
            " WHERE run_id = ? AND state = 'pending' RETURNING id",
            (_now(), run_id),
        ).fetchall()
        return [str(r["id"]) for r in rows]

    def _gate(self, row: sqlite3.Row) -> ReviewGate:
        body = ReviewGate.model_validate_json(row["body"])
        decided = row["decided_at"]
        return body.model_copy(
            update={
                "state": row["state"],
                "decided_at": datetime.fromisoformat(decided) if decided else None,
                "decided_by": row["decided_by"],
                "note": row["note"],
            }
        )


def _utc(when: datetime) -> str:
    """ISO text in UTC, so deadlines compare as strings."""
    return when.astimezone(UTC).isoformat()


class ImportResolvedError(ValueError):
    """The import decision isn't pending any more (IMPORT-UI-8: decided once)."""

    def __init__(self, decision_id: str, state: str) -> None:
        super().__init__(f"import decision {decision_id} is already {state}")
        self.decision_id = decision_id
        self.state = state


class ImportBusyError(ValueError):
    """A job is applying or looking up the decision right now."""

    def __init__(self, decision_id: str) -> None:
        super().__init__(f"import decision {decision_id} is being worked on; try again shortly")
        self.decision_id = decision_id


class AppImportDecisions:
    """``ImportDecisionStore`` backed by the ``import_decisions`` table (F-IMPORT-UI)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def add(self, decision: ImportDecision) -> None:
        self._conn.execute(
            "INSERT INTO import_decisions (id, profile, run_id, step_id, state, body,"
            " created_at, deadline) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.id,
                decision.profile,
                decision.run_id,
                decision.step_id,
                decision.state,
                decision.model_dump_json(),
                decision.created_at.isoformat(),
                _utc(decision.deadline) if decision.deadline is not None else None,
            ),
        )

    def save(self, decision: ImportDecision) -> None:
        self._conn.execute(
            "UPDATE import_decisions SET state = ?, body = ? WHERE id = ?",
            (decision.state, decision.model_dump_json(), decision.id),
        )

    def get(self, decision_id: str) -> ImportDecision | None:
        row = self._conn.execute(
            "SELECT body FROM import_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return ImportDecision.model_validate_json(row["body"]) if row else None

    def claimed(self, decision_id: str) -> bool:
        row = self._conn.execute(
            "SELECT claim FROM import_decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return row is not None and row["claim"] is not None

    def for_run(
        self, run_id: str, state: ImportDecisionState | None = None
    ) -> list[ImportDecision]:
        sql, args = "SELECT body FROM import_decisions WHERE run_id = ?", [run_id]
        if state is not None:
            sql, args = sql + " AND state = ?", [*args, state]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [ImportDecision.model_validate_json(r["body"]) for r in rows]

    def pending(
        self, profile: str | None = None, run_id: str | None = None
    ) -> list[ImportDecision]:
        """Pending decisions, oldest first (IMPORT-UI-7)."""
        sql, args = "SELECT body FROM import_decisions WHERE state = 'pending'", list[Any]()
        if profile is not None:
            sql, args = sql + " AND profile = ?", [*args, profile]
        if run_id is not None:
            sql, args = sql + " AND run_id = ?", [*args, run_id]
        rows = self._conn.execute(sql + " ORDER BY created_at, id", args).fetchall()
        return [ImportDecision.model_validate_json(r["body"]) for r in rows]

    def overdue(self, now: datetime) -> list[ImportDecision]:
        """Pending, unclaimed decisions whose deadline passed (IMPORT-UI-10)."""
        rows = self._conn.execute(
            "SELECT body FROM import_decisions WHERE state = 'pending' AND claim IS NULL"
            " AND deadline IS NOT NULL AND deadline <= ? ORDER BY deadline, id",
            (_utc(now),),
        ).fetchall()
        return [ImportDecision.model_validate_json(r["body"]) for r in rows]

    def next_deadline(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT MIN(deadline) FROM import_decisions WHERE state = 'pending'"
            " AND deadline IS NOT NULL"
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def claim(self, decision_id: str, job: str) -> ImportDecision:
        """Reserve a pending decision for one job. Raises ``KeyError`` (unknown),
        ``ImportResolvedError`` (not pending) or ``ImportBusyError`` (claimed)."""
        cur = self._conn.execute(
            "UPDATE import_decisions SET claim = ? WHERE id = ? AND state = 'pending'"
            " AND claim IS NULL",
            (job, decision_id),
        )
        decision = self.get(decision_id)
        if decision is None:
            raise KeyError(decision_id)
        if cur.rowcount != 1:
            if decision.state != "pending":
                raise ImportResolvedError(decision_id, decision.state)
            raise ImportBusyError(decision_id)
        return decision

    def set_claim(self, decision_id: str, job: str) -> None:
        """Hand a claim over to the job id it was reserved for."""
        self._conn.execute(
            "UPDATE import_decisions SET claim = ? WHERE id = ? AND claim IS NOT NULL",
            (job, decision_id),
        )

    def release(self, decision: ImportDecision) -> None:
        """Save ``decision`` and drop the claim (it stays pending, IMPORT-UI-13)."""
        self._conn.execute(
            "UPDATE import_decisions SET state = ?, body = ?, claim = NULL WHERE id = ?",
            (decision.state, decision.model_dump_json(), decision.id),
        )

    def cancel_for_run(self, run_id: str) -> list[str]:
        """Cancel the run's pending decisions (IMPORT-UI-14). Returns their ids."""
        cancelled: list[str] = []
        for decision in self.pending(run_id=run_id):
            decision.state = "cancelled"
            self.release(decision)
            cancelled.append(decision.id)
        return cancelled


class AppJournal:
    """``Journal`` backed by the app DB's ``journal`` table (JOURNAL-8)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def begin(self, entry: JournalEntry) -> None:
        if entry.state != "pending":
            raise JournalError(f"journal entry {entry.id} is {entry.state}, not pending")
        try:
            # Autocommit connection: the row is committed when execute returns (JOURNAL-1).
            self._conn.execute(
                "INSERT INTO journal (id, profile, changeset_id, run_id, state, body,"
                " created_at) VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (
                    entry.id,
                    entry.profile,
                    entry.changeset_id,
                    entry.run_id,
                    entry.model_dump_json(),
                    entry.created_at.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise JournalError(f"journal entry {entry.id} exists") from exc

    def complete(self, entry_id: str, result: ApplyResult) -> JournalEntry:
        return self._store(completed(self._get(entry_id), result, datetime.now(UTC)))

    def fail(self, entry_id: str, error: str) -> JournalEntry:
        return self._store(failed(self._get(entry_id), error, datetime.now(UTC)))

    def pending(self, profile: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT body FROM journal WHERE profile = ? AND state = 'pending'"
            " ORDER BY created_at, id",
            (profile,),
        ).fetchall()
        return [JournalEntry.model_validate_json(r["body"]) for r in rows]

    def get(self, entry_id: str) -> JournalEntry | None:
        row = self._conn.execute("SELECT body FROM journal WHERE id = ?", (entry_id,)).fetchone()
        return JournalEntry.model_validate_json(row["body"]) if row else None

    def for_run(self, run_id: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT body FROM journal WHERE run_id = ? ORDER BY created_at, id", (run_id,)
        ).fetchall()
        return [JournalEntry.model_validate_json(r["body"]) for r in rows]

    def for_changeset(self, changeset_id: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT body FROM journal WHERE changeset_id = ? ORDER BY created_at, id",
            (changeset_id,),
        ).fetchall()
        return [JournalEntry.model_validate_json(r["body"]) for r in rows]

    def mark_undone(self, entry_id: str, by: str) -> None:
        self._store(undone(self._get(entry_id), by))

    def recover(self, entry: JournalEntry) -> None:
        self._store(recovered(self._get(entry.id), entry))

    def uncertain(self, profile: str) -> list[JournalEntry]:
        rows = self._conn.execute(
            "SELECT body FROM journal WHERE profile = ? AND state = 'applied'"
            " AND json_extract(body, '$.uncertain') = 1 ORDER BY created_at, id",
            (profile,),
        ).fetchall()
        return [JournalEntry.model_validate_json(r["body"]) for r in rows]

    def _get(self, entry_id: str) -> JournalEntry:
        entry = self.get(entry_id)
        if entry is None:
            raise JournalError(f"no journal entry {entry_id}")
        return entry

    def _store(self, entry: JournalEntry) -> JournalEntry:
        self._conn.execute(
            "UPDATE journal SET state = ?, body = ?, completed_at = ?, undone_by = ? WHERE id = ?",
            (
                entry.state,
                entry.model_dump_json(),
                entry.completed_at.isoformat() if entry.completed_at else None,
                entry.undone_by,
                entry.id,
            ),
        )
        return entry


class AppTriggers:
    """Dedup records and webhook secret hashes (F-TRIGGERS TRIGGERS-17, -19 to -22)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    # -- dedup -----------------------------------------------------------------------

    def record(self, profile: str, workflow: str, input_path: str) -> DedupRecord | None:
        """The last run for this input of ``workflow``, with that run's current state."""
        row = self._conn.execute(
            "SELECT t.fingerprint, t.run_id, j.state FROM trigger_inputs t"
            " LEFT JOIN jobs j ON j.run_id = t.run_id AND j.kind = 'run'"
            " WHERE t.profile = ? AND t.workflow = ? AND t.input_path = ?",
            (profile, workflow, input_path),
        ).fetchone()
        if row is None:
            return None
        return DedupRecord(row["fingerprint"], row["run_id"], row["state"])

    def remember(
        self, profile: str, workflow: str, input_path: str, fingerprint: str, run_id: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO trigger_inputs (profile, workflow, input_path, fingerprint, run_id,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (profile, workflow, input_path) DO UPDATE SET"
            " fingerprint = excluded.fingerprint, run_id = excluded.run_id,"
            " updated_at = excluded.updated_at",
            (profile, workflow, input_path, fingerprint, run_id, _now()),
        )

    def active_run(self, profile: str, workflow: str, trigger: str) -> str | None:
        """A queued or running run of ``workflow`` started by ``trigger`` (TRIGGERS-14)."""
        row = self._conn.execute(
            "SELECT run_id FROM jobs WHERE kind = 'run' AND profile = ? AND workflow = ?"
            " AND trigger = ? AND state IN ('queued', 'running') ORDER BY created_at LIMIT 1",
            (profile, workflow, trigger),
        ).fetchone()
        return row["run_id"] if row else None

    def last_fired(self, profile: str, workflow: str, trigger: str) -> Job | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE kind = 'run' AND profile = ? AND workflow = ?"
            " AND trigger = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (profile, workflow, trigger),
        ).fetchone()
        return _job(row) if row else None

    # -- webhook secrets (hashes only) -----------------------------------------------

    def set_webhook_hash(self, profile: str, name: str, sha256: str) -> None:
        self._conn.execute(
            "INSERT INTO webhook_secrets (profile, name, sha256, created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (profile, name) DO UPDATE SET sha256 = excluded.sha256,"
            " created_at = excluded.created_at",
            (profile, name, sha256, _now()),
        )

    def webhook_hash(self, profile: str, name: str) -> str | None:
        row = self._conn.execute(
            "SELECT sha256 FROM webhook_secrets WHERE profile = ? AND name = ?", (profile, name)
        ).fetchone()
        return row["sha256"] if row else None

    def webhooks(self, profile: str | None = None) -> list[tuple[str, str, datetime]]:
        """``(profile, name, created_at)`` of every webhook that has a secret."""
        rows = self._conn.execute(
            "SELECT profile, name, created_at FROM webhook_secrets"
            " WHERE ? IS NULL OR profile = ? ORDER BY profile, name",
            (profile, profile),
        ).fetchall()
        return [(r["profile"], r["name"], datetime.fromisoformat(r["created_at"])) for r in rows]

    def delete_webhook(self, profile: str, name: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM webhook_secrets WHERE profile = ? AND name = ?", (profile, name)
        )
        return cur.rowcount > 0


class AppLanes:
    """A run's lanes (C-LANES ``run_lanes``) and resolved ``ctx.hold`` holds (LANES-14).

    Implements the engine's ``LaneStore``.
    """

    def __init__(self, db: AppDB) -> None:
        self.db = db

    @property
    def _conn(self) -> sqlite3.Connection:
        return self.db._conn  # pyright: ignore[reportPrivateUsage]

    def load(self, run_id: str) -> dict[str, Lane]:
        rows = self._conn.execute(
            "SELECT * FROM run_lanes WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return {row["key"]: _lane(row) for row in rows}

    def save(self, run_id: str, lanes: Iterable[Lane]) -> None:
        now = _now()
        with self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            for lane in lanes:
                self._conn.execute(
                    "INSERT INTO run_lanes (run_id, key, label, state, position, holds, steps,"
                    " passes, seq, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?,"
                    " (SELECT COALESCE(MAX(seq), 0) + 1 FROM run_lanes WHERE run_id = ?), ?)"
                    " ON CONFLICT (run_id, key) DO UPDATE SET label = excluded.label,"
                    " state = excluded.state, position = excluded.position,"
                    " holds = excluded.holds, steps = excluded.steps,"
                    " passes = excluded.passes, updated_at = excluded.updated_at",
                    (
                        run_id,
                        lane.key,
                        lane.label,
                        lane.state,
                        lane.position,
                        json.dumps([h.as_json() for h in lane.holds]),
                        json.dumps(lane.steps),
                        json.dumps(lane.passes),
                        run_id,
                        now,
                    ),
                )

    def rename(self, run_id: str, old: str, new: str, label: str | None = None) -> None:
        """LANES-14: keep position, holds and history; optionally a new label."""
        self._conn.execute(
            "UPDATE run_lanes SET key = ?, label = COALESCE(?, label), updated_at = ?"
            " WHERE run_id = ? AND key = ?",
            (new, label, _now(), run_id, old),
        )

    def set_state(
        self, run_id: str, keys: Iterable[str], state: str, *, only_from: Iterable[str]
    ) -> list[str]:
        """Move lanes ``keys`` that are in one of ``only_from`` to ``state`` (a final state
        clears position and holds). Returns the keys that moved."""
        moved: list[str] = []
        sources = list(only_from)
        marks = ",".join("?" for _ in sources)
        final = state in FINAL_LANE_STATES
        for key in keys:
            cur = self._conn.execute(
                "UPDATE run_lanes SET state = ?, updated_at = ?"
                + (", position = NULL, holds = '[]'" if final else "")
                + f" WHERE run_id = ? AND key = ? AND state IN ({marks})",
                (state, _now(), run_id, key, *sources),
            )
            if cur.rowcount == 1:
                moved.append(key)
        return moved

    def counts(self, run_ids: Iterable[str]) -> dict[str, dict[str, int]]:
        """Run id -> lane state -> count (LANES-9), in one query."""
        ids = list(run_ids)
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT run_id, state, COUNT(*) AS n FROM run_lanes WHERE run_id IN ({marks})"
            " GROUP BY run_id, state",
            ids,
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            out.setdefault(row["run_id"], {})[row["state"]] = int(row["n"])
        return out

    def resolve_hold(self, run_id: str, ref: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO resolved_holds (run_id, ref) VALUES (?, ?)", (run_id, ref)
        )

    def resolved_holds(self, run_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT ref FROM resolved_holds WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [row["ref"] for row in rows]


def _hold(value: Any) -> Hold:
    return Hold(value["kind"], str(value["ref"]))


def _lane(row: sqlite3.Row) -> Lane:
    return Lane(
        key=row["key"],
        label=row["label"],
        state=row["state"],
        position=row["position"],
        holds=[_hold(h) for h in json.loads(row["holds"])],
        steps={str(k): str(v) for k, v in json.loads(row["steps"]).items()},
        passes={str(k): int(v) for k, v in json.loads(row["passes"]).items()},
    )


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
    data["result"] = json.loads(data["result"]) if data.get("result") else None
    return Job.model_validate(data)
