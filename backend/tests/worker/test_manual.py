"""Manual ChangeSets from the tag grid as worker jobs (TAG-GRID-8, -9, -10, -11, -13)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppChangeSets, AppDB, AppJournal, SqliteEventSink
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.selection import Selection
from rifferr.engine import Event, StepTypeRegistry
from rifferr.engine.events import profile_stream
from rifferr.engine.manual import ManualChange, ManualChanges, check_manual, manual_changesets
from rifferr.profile_store import config_root, create_profile, load_profile
from rifferr.worker.process import Commands, Worker
from rifferr.worker.protocol import WorkerCommand, WorkerMessage
from rifferr.worker.readonly import ReadOnlyWorker, RequestFailed
from rifferr.worker.supervisor import WorkerSupervisor
from tests.engine.steps import ALL

STREAM = profile_stream("main")


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        create_profile(config_root(), "main")
        self.db = AppDB(tmp_path / "app.db")
        self.sink = SqliteEventSink(self.db)
        self.queue: Commands = asyncio.Queue()
        self.backend = FakeBackend()
        self.backend.add_album(
            {"album": "A", "year": 2001, "items": [{"title": "one"}, {"title": "two"}]}
        )
        self.backend.add_album({"album": "B", "year": 1990, "items": [{"title": "x"}]})
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry(ALL),
            self.backend,
            self._send,
        )
        self.supervisor = WorkerSupervisor(self.db, self.sink, command=lambda p: ["false"])
        self.seen: list[Event] = []
        self.supervisor.subscribe(self.seen.append)

        async def wake(profile: str) -> None:
            self.queue.put_nowait(WorkerCommand(kind="start_run", profile=profile))

        self.supervisor._wake = wake  # type: ignore[method-assign]
        self._ids = count(1)

    def _send(self, message: WorkerMessage) -> None:
        if message.event is not None:
            self.supervisor._persist(message.event)  # pyright: ignore[reportPrivateUsage]

    def album(self, name: str) -> Any:
        return next(a for a in self.backend.query(Selection()) if a.fields["album"] == name)

    def edit(self, name: str, *changes: FieldChange) -> ManualChange:
        a = self.album(name)
        return ManualChange(
            target=TargetRef(scope="album", id=a.id), album=a.id, changes=[*changes]
        )

    def changesets(self, *edits: ManualChange) -> list[ChangeSet]:
        checked = check_manual(self.backend.read_only_view(), ManualChanges(targets=[*edits]))
        assert checked.problems == []
        return manual_changesets(
            ManualChanges(targets=checked.targets),
            make_id=lambda: f"cs-{next(self._ids)}",
            profile="main",
            now=lambda: datetime.now(UTC),
        )

    def events(self) -> list[Event]:
        return self.db.events_since(STREAM, 0)

    def types(self) -> list[str]:
        return [e.type for e in self.events()]

    async def until(self, check: Any) -> None:
        async def poll() -> None:
            while not check():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 5)

    async def job_done(self, job_id: str) -> dict[str, Any]:
        def finished() -> bool:
            return any(
                e.type == "job.finished" and e.data["job_id"] == job_id for e in self.events()
            )

        await self.until(finished)
        return next(
            e.data for e in self.events() if e.type == "job.finished" and e.data["job_id"] == job_id
        )


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    task = asyncio.create_task(harness.worker.serve(harness.queue))
    try:
        yield harness
    finally:
        harness.queue.put_nowait(None)
        await asyncio.wait_for(task, 5)
        harness.db.close()


async def test_TAG_GRID_8_manual_changesets_apply_through_the_journal(h: Harness) -> None:
    changesets = h.changesets(
        h.edit("A", FieldChange(field="year", old=2001, new="2002")),
        h.edit("B", FieldChange(field="label", new="L")),
    )
    job_id, ahead = await h.supervisor.apply_manual("main", changesets, by="me")
    assert ahead == []
    done = await h.job_done(job_id)
    assert done["state"] == "succeeded"
    assert done["result"] == {"outcomes": {"cs-1": "applied", "cs-2": "applied"}}
    assert h.album("A").fields["year"] == 2002
    assert h.album("B").fields["label"] == "L"
    store = AppChangeSets(h.db)
    stored = store.get("cs-1")
    assert stored is not None
    assert (stored.state, stored.resolved_by) == ("applied", "me")
    assert stored.changeset.source == ChangeSource(kind="manual", name="ui")
    (entry,) = AppJournal(h.db).for_changeset("cs-1")
    assert (entry.state, entry.run_id) == ("applied", None)
    assert [s.changeset.id for s in store.manual("main", "ui", 10)] == ["cs-2", "cs-1"]


async def test_TAG_GRID_9_profile_stream_carries_changeset_events_in_order(h: Harness) -> None:
    (cs,) = h.changesets(h.edit("A", FieldChange(field="year", old=2001, new=2002)))
    job_id, _ = await h.supervisor.apply_manual("main", [cs])
    await h.job_done(job_id)
    events = h.events()
    assert [e.type for e in events] == ["changeset.applied", "job.finished"]
    assert [e.seq for e in events] == [1, 2]
    assert all(e.run_id == STREAM for e in events)
    assert events[0].data["changeset_id"] == cs.id
    assert [e.type for e in h.seen] == ["changeset.applied", "job.finished"]  # persisted, then sent


async def test_TAG_GRID_10_a_change_made_meanwhile_conflicts(h: Harness) -> None:
    (cs,) = h.changesets(h.edit("A", FieldChange(field="year", old=2001, new=2002)))
    album = h.album("A")
    h.backend.apply(  # someone else changes the year after the grid loaded it
        ChangeSet(
            id="other",
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
    job_id, _ = await h.supervisor.apply_manual("main", [cs])
    done = await h.job_done(job_id)
    assert done["result"] == {"outcomes": {cs.id: "conflicted"}}
    conflict = next(e for e in h.events() if e.type == "changeset.conflicted")
    assert conflict.data["conflicts"] == [
        {"target": f"album {album.id}", "field": "year", "expected": 2001, "current": 1999}
    ]
    assert h.album("A").fields["year"] == 1999


async def test_TAG_GRID_10_write_failures_are_reported(h: Harness) -> None:
    item = h.album("A").items[0]
    h.backend.fail_writes[item.id] = "file is read-only"
    (cs,) = h.changesets(
        ManualChange(
            target=TargetRef(scope="item", id=item.id),
            album=h.album("A").id,
            changes=[FieldChange(field="title", old="one", new="uno")],
        )
    )
    job_id, _ = await h.supervisor.apply_manual("main", [cs])
    await h.job_done(job_id)
    applied = next(e for e in h.events() if e.type == "changeset.applied")
    assert applied.data["write_failures"] == [
        {"target": f"item {item.id}", "path": item.path, "error": "file is read-only"}
    ]


async def test_TAG_GRID_11_undo_of_a_manual_edit_reports_on_the_profile_stream(
    h: Harness,
) -> None:
    (cs,) = h.changesets(h.edit("A", FieldChange(field="year", old=2001, new=2002)))
    job_id, _ = await h.supervisor.apply_manual("main", [cs])
    await h.job_done(job_id)
    (entry,) = AppJournal(h.db).for_changeset(cs.id)
    undo_job = await h.supervisor.undo(entry_id=entry.id, by="me")
    done = await h.job_done(undo_job)
    assert done["state"] == "succeeded"
    assert done["result"]["undone"] == [entry.id]
    assert "changeset.undone" in h.types()
    assert h.album("A").fields["year"] == 2001
    stored = AppChangeSets(h.db).get(cs.id)
    assert stored is not None
    assert stored.state == "undone"
    # The undo's own ChangeSet is manual / undo, not a grid edit.
    assert [s.changeset.id for s in AppChangeSets(h.db).manual("main", "ui", 10)] == [cs.id]


async def test_TAG_GRID_11_undo_stopped_by_a_conflict_says_so(h: Harness) -> None:
    (cs,) = h.changesets(h.edit("A", FieldChange(field="year", old=2001, new=2002)))
    job_id, _ = await h.supervisor.apply_manual("main", [cs])
    await h.job_done(job_id)
    (second,) = h.changesets(h.edit("A", FieldChange(field="year", old=2002, new=2003)))
    job_id, _ = await h.supervisor.apply_manual("main", [second])
    await h.job_done(job_id)
    (entry,) = AppJournal(h.db).for_changeset(cs.id)
    done = await h.job_done(await h.supervisor.undo(entry_id=entry.id))
    assert done["state"] == "failed"
    assert done["result"]["stopped_at"] == entry.id
    assert done["result"]["conflicts"][0]["current"] == 2003
    assert h.album("A").fields["year"] == 2003


async def test_TAG_GRID_13_edits_queue_behind_a_running_job(h: Harness) -> None:
    run = h.db.submit("main", {"source": "", "source_name": "wf.yaml", "vars": {}})
    claimed = h.db.claim_next("main")  # as if the worker were busy with it
    assert claimed is not None
    assert claimed.run_id == run.run_id
    (cs,) = h.changesets(h.edit("A", FieldChange(field="year", old=2001, new=2002)))
    job_id, ahead = await h.supervisor.apply_manual("main", [cs])
    assert ahead == [run.run]
    job = h.db.job(job_id)
    assert job is not None
    assert job.state == "queued"


async def test_TAG_GRID_9_a_crashed_worker_fails_the_edit_job_on_the_stream(
    tmp_path: Path,
) -> None:
    create_profile(config_root(), "main")
    db = AppDB(tmp_path / "app.db")
    supervisor = WorkerSupervisor(db, SqliteEventSink(db), command=lambda p: ["false"])
    cs = ChangeSet(
        id="cs-1",
        profile="main",
        source=ChangeSource(kind="manual", name="ui"),
        created_at=datetime.now(UTC),
        targets=[
            TargetChange(
                target=TargetRef(scope="album", id="a"), changes=[FieldChange(field="x", new="y")]
            )
        ],
    )
    job_id, _ = await supervisor.apply_manual("main", [cs])
    await supervisor.wait_idle_exit("main")
    (event,) = db.events_since(STREAM, 0)
    assert event.type == "job.finished"
    assert (event.data["job_id"], event.data["state"]) == (job_id, "failed")
    db.close()


# -- read-only worker requests (TAG-GRID-2, -7) ------------------------------------------


def test_TAG_GRID_2_read_only_worker_answers_library_queries(h: Harness) -> None:
    worker = ReadOnlyWorker(
        load_profile(config_root(), "main"), h.backend.read_only_view(), lambda m: None
    )
    page = worker.handle(
        {"kind": "library.query", "payload": {"query": "album:A", "scope": "item"}},
        lambda: False,
    )
    assert [a["fields"]["album"] for a in page["albums"]] == ["A"]  # type: ignore[index, union-attr]
    with pytest.raises(RequestFailed) as exc:
        worker.handle({"kind": "library.query", "payload": {"query": "bare"}}, lambda: False)
    assert exc.value.code == "bad_query"
    with pytest.raises(RequestFailed) as exc:
        worker.handle({"kind": "library.query", "payload": {"limit": 0}}, lambda: False)
    assert exc.value.code == "bad_request"


def test_TAG_GRID_7_read_only_worker_checks_edits(h: Harness) -> None:
    worker = ReadOnlyWorker(
        load_profile(config_root(), "main"), h.backend.read_only_view(), lambda m: None
    )
    edit = h.edit("A", FieldChange(field="year", old=2001, new="later"))
    result = worker.handle(
        {"kind": "changes.check", "payload": ManualChanges(targets=[edit]).model_dump(mode="json")},
        lambda: False,
    )
    (problem,) = result["problems"]  # type: ignore[misc]
    assert problem["field"] == "year"  # type: ignore[index, call-overload]
