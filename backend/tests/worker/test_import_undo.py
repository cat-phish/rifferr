"""Import undo in the worker (F-DASHBOARD DASHBOARD-7, -8, -9, -18)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppRemovals, AppTriggers
from rifferr.backends.base import RUN_MARKER
from rifferr.core.selection import Selection
from rifferr.core.triggers import dedup_skip
from rifferr.triggers import fs
from rifferr.worker.protocol import WorkerCommand
from tests.worker.conftest import wf
from tests.worker.test_review import RUN, Harness, serving

AUTO = wf("{id: a, uses: test.propose, apply: auto, with: {set: {label: L}}}")


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with serving(tmp_path) as harness:
        yield harness


def inbox_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "inbox" / "Artist - Album"
    folder.mkdir(parents=True)
    (folder / "01.flac").write_bytes(b"audio")
    return folder


def add(h: Harness, name: str, path: Path) -> str:
    return h.backend.add_album(
        {"album": name, RUN_MARKER: RUN, "path": str(path), "items": [{"title": "t"}]}
    ).id


async def run_and_undo(h: Harness, albums: list[str] | None = None) -> dict[str, Any]:
    h.submit(AUTO)
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await h.until(lambda: h.state() == "succeeded")
    job_id = await h.supervisor.import_undo(RUN, albums=albums, by="tester")
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await h.until(lambda: (j := h.db.job(job_id)) is not None and j.finished_at is not None)
    job = h.db.job(job_id)
    assert job is not None
    assert job.state == "succeeded", job.error
    assert isinstance(job.result, dict)
    return job.result


async def test_DASHBOARD_7_undo_import_undoes_changes_then_removes_albums(
    h: Harness, tmp_path: Path
) -> None:
    add(h, "A", tmp_path / "library" / "A")
    result = await run_and_undo(h)
    assert h.backend.query(Selection()) == []
    removed = result["removed"]
    assert isinstance(removed, list)
    assert [r["label"] for r in removed] == ["A"]
    # The run's tag change was undone through the journal first (JOURNAL-6).
    (entry, undo) = h.journal.for_run(RUN)
    assert entry.undone_by == undo.id


async def test_DASHBOARD_7_undo_import_of_some_albums(h: Harness, tmp_path: Path) -> None:
    a = add(h, "A", tmp_path / "library" / "A")
    add(h, "B", tmp_path / "library" / "B")
    await run_and_undo(h, albums=[a])
    assert [x.fields["album"] for x in h.backend.query(Selection())] == ["B"]


async def test_DASHBOARD_7_refused_while_the_run_is_queued(h: Harness) -> None:
    h.submit(AUTO)
    with pytest.raises(ValueError, match="queued"):
        await h.supervisor.import_undo(RUN)
    with pytest.raises(KeyError):
        await h.supervisor.import_undo("nosuchrun")


async def test_DASHBOARD_8_each_removal_is_journaled(h: Harness, tmp_path: Path) -> None:
    add(h, "A", tmp_path / "library" / "A")
    await run_and_undo(h)
    (record,) = AppRemovals(h.db).for_run(RUN)
    assert (record.label, record.state, record.delete_files) == ("A", "removed", False)


async def test_DASHBOARD_18_files_are_kept_without_a_recorded_intact_input(
    h: Harness, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    h.backend.music_dirs = [library]
    add(h, "A", library / "A")
    result = await run_and_undo(h)
    assert h.backend.removed[-1][1] is False
    assert h.backend.deleted == []
    (removed,) = result["removed"]  # type: ignore[misc]
    assert removed["files_at"] == str(library / "A")


async def test_DASHBOARD_18_copies_in_the_library_are_deleted(h: Harness, tmp_path: Path) -> None:
    library = tmp_path / "library"
    h.backend.music_dirs = [library]
    source = inbox_folder(tmp_path)
    fingerprint = fs.input_fingerprint(source)
    assert fingerprint is not None
    AppTriggers(h.db).remember("main", "wf", str(source), fingerprint, RUN)
    add(h, "A", library / "A")
    await run_and_undo(h)
    assert h.backend.removed[-1][1] is True
    assert h.backend.deleted == [str(library / "A" / "t.flac")]


async def test_DASHBOARD_18_files_inside_the_input_folder_are_never_deleted(
    h: Harness, tmp_path: Path
) -> None:
    source = inbox_folder(tmp_path)
    h.backend.music_dirs = [tmp_path]  # even when the library folder contains the inbox
    fingerprint = fs.input_fingerprint(source)
    assert fingerprint is not None
    AppTriggers(h.db).remember("main", "wf", str(source), fingerprint, RUN)
    add(h, "A", source)  # imported in place
    await run_and_undo(h)
    assert h.backend.removed[-1][1] is False
    assert (source / "01.flac").exists()


async def test_DASHBOARD_9_undone_input_is_kept_and_never_refired(
    h: Harness, tmp_path: Path
) -> None:
    source = inbox_folder(tmp_path)
    fingerprint = fs.input_fingerprint(source)
    assert fingerprint is not None
    store = AppTriggers(h.db)
    store.remember("main", "wf", str(source), fingerprint, RUN)
    add(h, "A", tmp_path / "library" / "A")
    await run_and_undo(h)
    record = store.record("main", "wf", str(source))
    assert record is not None
    assert record.undone
    assert dedup_skip(record, fingerprint) == "undone"
    assert dedup_skip(record, fingerprint, force=True) is None
    store.remember("main", "wf", str(source), fingerprint, "01NEWRUN")
    again = store.record("main", "wf", str(source))
    assert again is not None
    assert not again.undone


def test_DASHBOARD_8_interrupted_removals_are_settled_at_start(h: Harness, tmp_path: Path) -> None:
    from rifferr.worker.import_undo import recover_removals

    still = add(h, "A", tmp_path / "library" / "A")
    removals = AppRemovals(h.db)
    left = removals.begin("main", RUN, still, "A", False)
    gone = removals.begin("main", RUN, "album:gone", "B", False)
    assert recover_removals(h.db, h.backend, "main") == [left.id, gone.id]
    by_id = {r.id: r for r in removals.for_run(RUN)}
    assert (by_id[left.id].state, by_id[gone.id].state) == ("failed", "removed")
    assert h.backend.query(Selection())  # recovery never removes anything
