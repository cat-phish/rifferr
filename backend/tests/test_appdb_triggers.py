"""F-TRIGGERS app DB: dedup records, scheduled-run lookups, webhook secret hashes."""

from __future__ import annotations

from pathlib import Path

from rifferr.appdb import AppDB, AppTriggers
from rifferr.core.triggers import DedupRecord


def set_state(db: AppDB, run_id: str, state: str) -> None:
    db._conn.execute(  # pyright: ignore[reportPrivateUsage]
        "UPDATE jobs SET state = ? WHERE run_id = ?", (state, run_id)
    )


def make(tmp_path: Path) -> tuple[AppDB, AppTriggers]:
    db = AppDB(tmp_path / "rifferr.db")
    return db, AppTriggers(db)


def test_TRIGGERS_22_dedup_record_has_the_runs_current_state(tmp_path: Path) -> None:
    db, t = make(tmp_path)
    job = db.submit("main", {"source": ""}, workflow="wf", trigger="watch", input="/in/A")
    assert (job.trigger, job.input) == ("watch", "/in/A")
    t.remember("main", "wf", "/in/A", "fp1", job.run)
    assert t.record("main", "wf", "/in/A") == DedupRecord("fp1", job.run, "queued")
    set_state(db, job.run, "succeeded")
    assert t.record("main", "wf", "/in/A") == DedupRecord("fp1", job.run, "succeeded")
    assert t.record("main", "other", "/in/A") is None
    assert t.record("other", "wf", "/in/A") is None


def test_TRIGGERS_22_dedup_record_is_replaced_and_survives_reopen(tmp_path: Path) -> None:
    db, t = make(tmp_path)
    t.remember("main", "wf", "/in/A", "fp1", "r1")
    t.remember("main", "wf", "/in/A", "fp2", "r2")
    db.close()
    t2 = AppTriggers(AppDB(tmp_path / "rifferr.db"))
    assert t2.record("main", "wf", "/in/A") == DedupRecord("fp2", "r2", None)


def test_TRIGGERS_14_active_scheduled_run(tmp_path: Path) -> None:
    db, t = make(tmp_path)
    watch = db.submit("main", {}, workflow="wf", trigger="watch")
    assert t.active_run("main", "wf", "schedule") is None
    sched = db.submit("main", {}, workflow="wf", trigger="schedule")
    assert t.active_run("main", "wf", "schedule") == sched.run
    set_state(db, sched.run, "failed")
    assert t.active_run("main", "wf", "schedule") is None
    last = t.last_fired("main", "wf", "schedule")
    assert last is not None
    assert last.run == sched.run
    assert t.active_run("main", "wf", "watch") == watch.run


def test_TRIGGERS_17_webhook_hashes(tmp_path: Path) -> None:
    _, t = make(tmp_path)
    assert t.webhook_hash("main", "h") is None
    t.set_webhook_hash("main", "h", "a" * 64)
    t.set_webhook_hash("main", "h", "b" * 64)
    t.set_webhook_hash("other", "h", "c" * 64)
    assert t.webhook_hash("main", "h") == "b" * 64
    assert [(p, n) for p, n, _ in t.webhooks()] == [("main", "h"), ("other", "h")]
    assert [(p, n) for p, n, _ in t.webhooks("main")] == [("main", "h")]
    assert t.delete_webhook("main", "h") is True
    assert t.delete_webhook("main", "h") is False
