"""Undo jobs and start-up recovery in the worker (C-JOURNAL JOURNAL-3 .. -6)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rifferr.core.changeset import (
    ChangeSet,
    ChangeSource,
    FieldChange,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from rifferr.core.selection import Selection
from rifferr.worker.protocol import WorkerCommand
from tests.worker.conftest import wf
from tests.worker.test_review import RUN, Harness, _suspended, serving


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with serving(tmp_path) as harness:
        yield harness


AUTO = wf(
    "{id: a, uses: test.propose, apply: auto, with: {set: {label: L}}}",
    "{id: b, uses: test.propose, apply: auto, with: {set: {year: 2002}}}",
)


async def _applied_run(h: Harness) -> None:
    h.album("A")
    h.submit(AUTO)
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await h.until(lambda: h.state() == "succeeded")


def _album(h: Harness) -> dict[str, object]:
    return dict(h.backend.query(Selection())[0].fields)


async def _finished(h: Harness, job_id: str) -> None:
    await h.until(lambda: (j := h.db.job(job_id)) is not None and j.finished_at is not None)


async def test_JOURNAL_6_undo_run_job_reverts_the_run_newest_first(h: Harness) -> None:
    await _applied_run(h)
    assert _album(h)["label"] == "L"
    first, second = h.journal.for_run(RUN)
    job_id = await h.supervisor.undo(run_id=RUN, by="tester")
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await _finished(h, job_id)
    job = h.db.job(job_id)
    assert job is not None
    assert job.state == "succeeded"
    assert isinstance(job.result, dict)
    assert job.result["undone"] == [second.id, first.id]
    assert job.result["stopped_at"] is None
    assert "label" not in _album(h)
    assert _album(h)["year"] == 2001
    assert [e.type for e in h.db.events_since(RUN, 0)].count("changeset.undone") == 2


async def test_JOURNAL_5_undo_entry_job_reports_conflicts(h: Harness) -> None:
    await _applied_run(h)
    first, _ = h.journal.for_run(RUN)
    album = h.backend.query(Selection())[0]
    h.backend.apply(  # someone edits the label after the run
        ChangeSet(
            id="outside",
            profile="main",
            source=ChangeSource(kind="manual", name="t"),
            created_at=datetime.now(UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id=album.id),
                    changes=[FieldChange(field="label", old="L", new="M")],
                )
            ],
        )
    )
    job_id = await h.supervisor.undo(entry_id=first.id)
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await _finished(h, job_id)
    job = h.db.job(job_id)
    assert job is not None
    assert job.state == "failed"
    assert job.error == f"undo stopped at journal entry {first.id}"
    assert isinstance(job.result, dict)
    conflicts = job.result["conflicts"]
    assert isinstance(conflicts, list)
    assert conflicts[0] == {
        "target": f"album {album.id}",
        "field": "label",
        "expected": "L",
        "current": "M",
        "would_restore": None,
    }
    assert _album(h)["label"] == "M"


async def test_JOURNAL_6_supervisor_validates_undo_requests(h: Harness) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        await h.supervisor.undo()
    with pytest.raises(KeyError):
        await h.supervisor.undo(entry_id="missing")
    with pytest.raises(KeyError):
        await h.supervisor.undo(run_id="missing")
    await _suspended(h, "A")  # a waiting run with nothing applied yet
    with pytest.raises(ValueError, match="no applied changes"):
        await h.supervisor.undo(run_id=RUN)


async def test_JOURNAL_5_supervisor_refuses_entries_already_undone(h: Harness) -> None:
    await _applied_run(h)
    first, _ = h.journal.for_run(RUN)
    job_id = await h.supervisor.undo(entry_id=first.id)
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await _finished(h, job_id)
    with pytest.raises(ValueError, match="already undone"):
        await h.supervisor.undo(entry_id=first.id)


async def test_JOURNAL_3_worker_start_up_settles_pending_entries(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.album("A")
    harness.submit()
    first = asyncio.create_task(harness.worker.serve(harness.queue))
    await harness.until(lambda: harness.state() == "waiting_review")
    harness.queue.put_nowait(None)
    await asyncio.wait_for(first, 5)
    # A crash between begin and apply: the entry is pending, nothing changed.
    (stored,) = harness.changesets.for_run(RUN, "proposed")
    cs = stored.changeset
    harness.journal.begin(
        JournalEntry(
            id="crashed",
            profile="main",
            changeset_id=cs.id,
            run_id=RUN,
            intended=cs.targets,
            created_at=datetime.now(UTC),
        )
    )
    harness.changesets.set_state(cs.id, "applied")  # what the worker had got to
    harness.queue = asyncio.Queue()
    second = asyncio.create_task(harness.worker.serve(harness.queue))
    await harness.until(lambda: harness.journal.pending("main") == [])
    entry = harness.journal.get("crashed")
    assert entry is not None
    assert (entry.state, entry.error) == ("failed", "interrupted before apply")
    again = harness.changesets.get(cs.id)
    assert again is not None
    assert again.state == "proposed"
    assert "label" not in _album(harness)
    harness.queue.put_nowait(None)
    await asyncio.wait_for(second, 5)
    harness.db.close()
