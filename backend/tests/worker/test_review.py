"""Review decisions as worker jobs, suspension and resume (CHANGESET-7, -9, -13, RUN-14)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppChangeSets, AppDB, AppJournal, SqliteEventSink
from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, create_profile, load_profile
from rifferr.worker.process import Commands, Worker
from rifferr.worker.protocol import WorkerCommand, WorkerMessage
from rifferr.worker.supervisor import WorkerSupervisor
from tests.engine.steps import ALL
from tests.worker.conftest import wf

RUN = "01RUNREVIEW000000000000001"
REVIEW_THEN_ECHO = wf(
    "{id: p, uses: test.propose, with: {set: {label: L, year: 2002}}}",
    "{id: after, uses: test.echo, with: {message: 'resumed with {{ steps.p.proposed }}'}}",
)


class Harness:
    """An in-process worker whose events are stored like the server stores them."""

    def __init__(self, tmp_path: Path) -> None:
        create_profile(config_root(), "main")
        self.db = AppDB(tmp_path / "app.db")
        self.sink = SqliteEventSink(self.db)
        self.sent: list[WorkerMessage] = []
        self.queue: Commands = asyncio.Queue()
        self.backend = FakeBackend()
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry(ALL),
            self.backend,
            self._send,
        )
        self.changesets = AppChangeSets(self.db)
        self.journal = AppJournal(self.db)
        self.supervisor = WorkerSupervisor(self.db, self.sink, command=lambda p: ["false"])

    def _send(self, message: WorkerMessage) -> None:
        self.sent.append(message)
        if message.event is not None:
            self.sink.append(message.event)

    def album(self, name: str, **fields: Any) -> None:
        self.backend.add_album(
            {"album": name, "year": 2001, RUN_MARKER: RUN, **fields, "items": [{"title": "t"}]}
        )

    def submit(self, source: str = REVIEW_THEN_ECHO) -> None:
        payload: dict[str, Any] = {"source": source, "source_name": "wf.yaml", "vars": {}}
        self.db.submit("main", payload, run_id=RUN)

    def decide(self, changeset_id: str, decision: str, accepted: list[int] | None = None) -> str:
        payload: dict[str, Any] = {
            "changeset_id": changeset_id,
            "decision": decision,
            "accepted": accepted,
            "by": "tester",
        }
        job = self.db.submit_job("main", "review_decision", payload, RUN)
        self.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
        return job.id

    def types(self) -> list[str]:
        return [e.type for e in self.db.events_since(RUN, 0)]

    def state(self) -> str:
        job = self.db.job_for_run(RUN)
        assert job is not None
        return job.state

    async def until(self, check: Any) -> None:
        async def poll() -> None:
            while not check():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 5)

    def pending(self) -> list[str]:
        return [s.changeset.id for s in self.changesets.for_run(RUN, "proposed")]

    def labels(self) -> dict[str, Any]:
        return {
            str(a.fields["album"]): a.fields.get("label") for a in self.backend.query(Selection())
        }


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    task = asyncio.create_task(harness.worker.serve(harness.queue))
    yield harness
    harness.queue.put_nowait(None)
    await asyncio.wait_for(task, 5)
    harness.db.close()


async def _suspended(h: Harness, *albums: str) -> list[str]:
    for name in albums:
        h.album(name)
    h.submit()
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await h.until(lambda: h.state() == "waiting_review")
    return h.pending()


async def test_CHANGESET_7_suspended_run_releases_the_worker_and_keeps_its_holds(
    h: Harness,
) -> None:
    ids = await _suspended(h, "A", "B")
    assert len(ids) == 2
    job = h.db.job_for_run(RUN)
    assert job is not None
    assert job.payload["resume_after"] == "p"
    assert job.payload["holds"] == ids
    assert h.types()[-1] == "run.waiting_review"
    assert "run.finished" not in h.types()
    assert [j.state for j in h.db.jobs("main")] == ["waiting_review"]  # nothing running


async def test_CHANGESET_13_accept_is_a_worker_job_that_applies_and_resumes(h: Harness) -> None:
    (cs_id,) = await _suspended(h, "A")
    job_id = h.decide(cs_id, "accept")
    await h.until(lambda: h.state() == "succeeded")
    decision = h.db.job(job_id)
    assert decision is not None
    assert decision.state == "succeeded"
    stored = h.changesets.get(cs_id)
    assert stored is not None
    assert (stored.state, stored.resolved_by) == ("applied", "tester")
    assert h.labels() == {"A": "L"}
    (entry,) = h.journal.for_run(RUN)
    assert entry.state == "applied"
    types = h.types()
    assert types.index("changeset.applied") < types.index("run.resumed")
    assert types[-1] == "run.finished"
    assert "resumed with 1" in [
        e.data.get("line") for e in h.db.events_since(RUN, 0) if e.type == "run.step.log"
    ]
    seqs = [e.seq for e in h.db.events_since(RUN, 0)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_CHANGESET_9_partial_accept_applies_only_the_accepted_field_changes(
    h: Harness,
) -> None:
    (cs_id,) = await _suspended(h, "A")
    stored = h.changesets.get(cs_id)
    assert stored is not None
    assert [c.field for _, c in stored.changeset.field_changes()] == ["label", "year"]
    h.decide(cs_id, "accept", accepted=[1])
    await h.until(lambda: h.state() == "succeeded")
    after = h.changesets.get(cs_id)
    assert after is not None
    assert after.state == "partially_applied"
    assert after.decisions == {0: False, 1: True}
    album = h.backend.query(Selection())[0]
    assert album.fields["year"] == 2002
    assert "label" not in album.fields
    (entry,) = h.journal.for_run(RUN)
    assert entry.accepted == [1]


async def test_CHANGESET_7_run_resumes_only_after_the_last_hold_is_resolved(
    h: Harness,
) -> None:
    first, second = await _suspended(h, "A", "B")
    h.decide(first, "reject")
    await h.until(lambda: first not in h.pending())
    await asyncio.sleep(0.05)
    assert h.state() == "waiting_review"
    h.decide(second, "accept")
    await h.until(lambda: h.state() == "succeeded")
    assert h.labels() == {"A": None, "B": "L"}
    rejected = h.changesets.get(first)
    assert rejected is not None
    assert rejected.state == "rejected"


async def test_CHANGESET_8_conflict_in_review_keeps_the_run_waiting(h: Harness) -> None:
    (cs_id,) = await _suspended(h, "A")
    album = h.backend.query(Selection())[0]
    h.backend.apply(  # someone edits the album while the ChangeSet waits for review
        ChangeSet(
            id="manual",
            profile="main",
            source=ChangeSource(kind="manual", name="test"),
            created_at=datetime.now(UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id=album.id),
                    changes=[FieldChange(field="year", old=2001, new=1999)],
                )
            ],
        )
    )
    h.decide(cs_id, "accept")
    await h.until(lambda: "changeset.conflicted" in h.types())
    stored = h.changesets.get(cs_id)
    assert stored is not None
    assert stored.state == "conflicted"
    assert h.state() == "waiting_review"  # a conflict still holds the run
    h.decide(cs_id, "reject")
    await h.until(lambda: h.state() == "succeeded")


async def test_CHANGESET_13_decisions_on_resolved_changesets_fail(h: Harness) -> None:
    (cs_id,) = await _suspended(h, "A")
    h.decide(cs_id, "reject")
    await h.until(lambda: h.state() == "succeeded")
    again = h.decide(cs_id, "accept")
    await h.until(lambda: (j := h.db.job(again)) is not None and j.state == "failed")
    job = h.db.job(again)
    assert job is not None
    assert "already rejected" in str(job.error)


async def test_CHANGESET_13_supervisor_validates_decisions(h: Harness) -> None:
    (cs_id,) = await _suspended(h, "A")
    with pytest.raises(KeyError):
        await h.supervisor.decide("nope", accept=True)
    with pytest.raises(ValueError, match="indexes"):
        await h.supervisor.decide(cs_id, accept=True, accepted=[5])
    with pytest.raises(ValueError, match="indexes"):
        await h.supervisor.decide(cs_id, accept=True, accepted=[])


async def test_CHANGESET_7_cancel_while_waiting_rejects_pending_changesets(
    h: Harness,
) -> None:
    ids = await _suspended(h, "A", "B")
    await h.supervisor.cancel(RUN)
    assert h.state() == "cancelled"
    assert [s.state for s in h.changesets.for_run(RUN)] == ["rejected", "rejected"]
    events = h.db.events_since(RUN, 0)
    assert [e.data["changeset_id"] for e in events if e.type == "changeset.rejected"] == ids
    assert events[-1].type == "run.finished"
    assert events[-1].data == {"status": "cancelled"}
    assert h.labels() == {"A": None, "B": None}
