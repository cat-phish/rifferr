"""S-FILES-CLEANUP in the worker and supervisor: busy folders, review decisions, timeout,
cancel (FILES-CLEANUP-7, -14)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppCleanupDecisions, AppLanes, CleanupResolvedError
from rifferr.core.files import CleanupDecision
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.files_cleanup import FilesCleanupStep
from rifferr.worker.cleanup import busy_folders
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

AFTER = "{id: after, uses: test.echo, with: {message: after cleanup}}"


class CleanupHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, FilesCleanupStep()]),
            self.backend,
            self._send,
        )
        self.cleanups = AppCleanupDecisions(self.db)
        self.inbox = tmp_path / "inbox"
        (self.inbox / "Album").mkdir(parents=True)
        (self.inbox / "Album" / "info.nfo").write_text("nfo")
        (self.inbox / "Album" / "notes.txt").write_text("keep")

    def submit_trusted(self, source: str) -> None:
        payload: dict[str, Any] = {
            "source": source,
            "source_name": "wf.yaml",
            "vars": {},
            "trusted": True,
        }
        self.db.submit("main", payload, run_id=RUN)
        from rifferr.worker.protocol import WorkerCommand

        self.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))

    async def waiting_cleanup(self, **params: Any) -> CleanupDecision:
        step = {"path": str(self.inbox), "mode": "delete", "delete": ["*.nfo"], "review": True}
        source = wf(
            "{id: clean, uses: files.cleanup, with: " + json.dumps({**step, **params}) + "}",
            AFTER,
        )
        self.submit_trusted(source)
        await self.until(lambda: self.state() == "waiting_review")
        (decision,) = self.cleanups.pending(run_id=RUN)
        return decision

    async def job_done(self, job_id: str) -> str:
        await self.until(
            lambda: (j := self.db.job(job_id)) is not None and j.state in ("succeeded", "failed")
        )
        job = self.db.job(job_id)
        assert job is not None
        return job.state


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[CleanupHarness]:
    harness = CleanupHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def test_FILES_CLEANUP_14_review_leaves_a_decision_and_waits(h: CleanupHarness) -> None:
    decision = await h.waiting_cleanup()
    assert (decision.state, decision.path) == ("pending", str(h.inbox))
    assert [e.path for e in decision.entries if e.cls == "delete"] == ["Album/info.nfo"]
    (lane,) = AppLanes(h.db).load(RUN).values()
    assert (lane.key, lane.state) == (decision.lane_key, "waiting")
    (event,) = h.events("cleanup.requested")
    assert event.data["decision_id"] == decision.id
    assert (h.inbox / "Album" / "info.nfo").exists()


async def test_FILES_CLEANUP_14_delete_cleans_then_the_run_goes_on(h: CleanupHarness) -> None:
    decision = await h.waiting_cleanup()
    job = await h.supervisor.decide_cleanup(decision.id, "delete", by="ui", note="ok")
    assert job is not None
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert not (h.inbox / "Album" / "info.nfo").exists()
    assert (h.inbox / "Album" / "notes.txt").exists()
    done = h.cleanups.get(decision.id)
    assert done is not None
    assert (done.state, done.decided_by, done.note) == ("deleted", "ui", "ok")
    assert done.result is not None
    assert done.result["deleted"] == 1
    (resolved,) = h.events("cleanup.resolved")
    assert (resolved.data["decision"], resolved.data["decided_by"]) == ("delete", "ui")
    assert "after cleanup" in h.logs()
    with pytest.raises(CleanupResolvedError):
        await h.supervisor.decide_cleanup(decision.id, "keep")


async def test_FILES_CLEANUP_14_keep_deletes_nothing(h: CleanupHarness) -> None:
    decision = await h.waiting_cleanup()
    assert await h.supervisor.decide_cleanup(decision.id, "keep", by="ui") is None
    await h.until(lambda: h.state() == "succeeded")
    assert (h.inbox / "Album" / "info.nfo").exists()
    done = h.cleanups.get(decision.id)
    assert done is not None
    assert done.state == "kept"
    assert "after cleanup" in h.logs()


async def test_FILES_CLEANUP_14_timeout_decides_on_timeout(h: CleanupHarness) -> None:
    decision = await h.waiting_cleanup(timeout="1h", on_timeout="delete")
    assert decision.deadline is not None
    assert await h.supervisor.decide_overdue_cleanups() == []
    h.now = decision.deadline + timedelta(seconds=1)
    assert await h.supervisor.decide_overdue_cleanups() == [decision.id]
    await h.until(lambda: h.state() == "succeeded")
    done = h.cleanups.get(decision.id)
    assert done is not None
    assert (done.state, done.decided_by) == ("deleted", "timeout")


async def test_FILES_CLEANUP_14_cancel_cancels_the_decision(h: CleanupHarness) -> None:
    decision = await h.waiting_cleanup()
    await h.supervisor.cancel(RUN)
    assert h.state() == "cancelled"
    done = h.cleanups.get(decision.id)
    assert done is not None
    assert done.state == "cancelled"
    with pytest.raises(CleanupResolvedError):
        await h.supervisor.decide_cleanup(decision.id, "delete")
    assert (h.inbox / "Album" / "info.nfo").exists()


async def test_FILES_CLEANUP_7_busy_folders_from_decisions_and_other_runs(
    h: CleanupHarness,
) -> None:
    other = h.db.submit("main", {"source": "", "input": "/in/other"}, run_id="01OTHERRUN")
    h.db.submit("main", {"source": "", "input": "/in/mine"}, run_id="01MINE")
    busy = busy_folders(h.db, "main", "01MINE")
    assert [(str(b.path), b.reason) for b in busy] == [
        ("/in/other", f"input of run {other.run_id}")
    ]
