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
from rifferr.core.review import GateState, ReviewGate
from rifferr.engine.changesets import StoredChangeSet
from rifferr.engine.events import Event
from rifferr.engine.journal import JournalError, completed, failed, recovered, undone

JobState = Literal["queued", "running", "waiting_review", "succeeded", "failed", "cancelled"]
#: ``run``: the run record itself (API-6). ``review_decision``: apply or reject a
#: ChangeSet (CHANGESET-13). ``undo``: undo a journal entry or a run (C-JOURNAL).
#: ``review_reject``: undo a run rejected at a review gate, then end it (REVIEW-8).
#: ``manual_apply``: apply manual ChangeSets from the tag grid (TAG-GRID-8).
#: ``repropose``: propose a conflicted ChangeSet again on current values (REVIEW-QUEUE-5).
JobKind = Literal["run", "review_decision", "undo", "review_reject", "manual_apply", "repropose"]

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
        """Queue a waiting run once nothing holds it (RUN-14): none of its ChangeSet holds
        is still proposed or conflicted, and every gate it waits for was continued.

        One transaction, so two decisions finishing at once can't both see the other
        still pending and leave the run waiting. True if the run was queued.
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
            payload = json.loads(row["payload"])
            holds = [str(h) for h in payload.get("holds") or []]
            gates = [str(g) for g in payload.get("gates") or []]
            if holds:
                marks = ",".join("?" for _ in holds)
                held = self._conn.execute(
                    f"SELECT 1 FROM changesets WHERE id IN ({marks})"
                    f" AND state IN {HOLDING_CHANGESETS} LIMIT 1",
                    holds,
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
            cur = self._conn.execute(
                "UPDATE jobs SET state = 'queued', claimed_at = NULL"
                " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
                (run_id,),
            )
            return cur.rowcount == 1

    def requeue(self, run_id: str) -> bool:
        """waiting_review -> queued, so a worker resumes the run (RUN-14)."""
        cur = self._conn.execute(
            "UPDATE jobs SET state = 'queued', claimed_at = NULL"
            " WHERE run_id = ? AND kind = 'run' AND state = 'waiting_review'",
            (run_id,),
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
