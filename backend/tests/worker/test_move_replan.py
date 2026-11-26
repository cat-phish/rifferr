"""A move waiting in review follows its album (S-LIBRARY-MOVE LIBRARY-MOVE-19, B-097)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.library_move import LibraryMoveStep
from rifferr.steps.review import ReviewStep
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

MOVE = wf(
    "{id: final-review, uses: library.move, with: {directory: /music}}",
    "{id: after, uses: test.echo, with: {message: 'after move'}}",
)


class ReplanHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), LibraryMoveStep()]),
            self.backend,
            self._send,
        )

    def album_id(self) -> str:
        [album] = self.backend.query(Selection())
        return album.id

    def paths(self) -> list[str]:
        [album] = self.backend.query(Selection())
        return sorted(i.path for i in album.items)

    def waiting_move(self) -> ChangeSet:
        (stored,) = self.changesets.for_run(RUN, "proposed")
        return stored.changeset

    async def edit_album(self, field: str, old: str, new: str) -> None:
        cs = ChangeSet(
            id=f"manual-{field}",
            profile="main",
            source=ChangeSource(kind="manual", name="ui"),
            created_at=datetime(2026, 11, 25, tzinfo=UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id=self.album_id()),
                    changes=[FieldChange(field=field, old=old, new=new)],
                )
            ],
        )
        job_id, _ = await self.supervisor.apply_manual("main", [cs], by="ui")
        await self.until(
            lambda: (j := self.db.job(job_id)) is not None and j.state in ("succeeded", "failed")
        )


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[ReplanHarness]:
    harness = ReplanHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def waiting(h: ReplanHarness) -> ChangeSet:
    album = h.backend.add_album(
        {
            "albumartist": "The Beatles",
            "album": "Abbey Road",
            RUN_MARKER: RUN,
            "path": "/inbox/Abbey Road",
            "items": [
                {"title": "Come Together", "track": 1},
                {"title": "Something", "track": 2},
            ],
        }
    )
    h.backend.album_files(album.id)
    h.submit(MOVE)
    await h.until(lambda: h.state() == "waiting_review")
    return h.waiting_move()


async def test_LIBRARY_MOVE_19_an_edit_replans_the_waiting_move(h: ReplanHarness) -> None:
    before = await waiting(h)
    assert before.replan is not None
    assert all("/Abbey Road/" in f.dst for f in before.files)
    await h.edit_album("album", "Abbey Road", "Let It Be")
    after = h.waiting_move()
    assert after.id == before.id  # the same review item
    assert all("/Let It Be/" in f.dst for f in after.files)
    assert h.events("changeset.replanned")
    await h.supervisor.decide(after.id, accept=True, by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert all(p.startswith("/music/The Beatles/Let It Be/") for p in h.paths())
    assert h.logs()[-1] == "after move"


async def test_LIBRARY_MOVE_19_accept_replans_a_stale_move(h: ReplanHarness) -> None:
    await waiting(h)
    # A change the worker wasn't told about (e.g. a `beet modify`).
    outside = ChangeSet(
        id="outside",
        profile="main",
        source=ChangeSource(kind="manual", name="cli"),
        created_at=datetime(2026, 11, 25, tzinfo=UTC),
        targets=[
            TargetChange(
                target=TargetRef(scope="album", id=h.album_id()),
                changes=[FieldChange(field="album", old="Abbey Road", new="Help!")],
            )
        ],
    )
    assert h.backend.apply(outside).applied
    cs = h.waiting_move()
    await h.supervisor.decide(cs.id, accept=True, by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert all(p.startswith("/music/The Beatles/Help!/") for p in h.paths())


async def test_LIBRARY_MOVE_19_a_conflicting_replan_moves_nothing(h: ReplanHarness) -> None:
    await waiting(h)
    h.backend.add_files({"/music/The Beatles/Let It Be/1 Come Together.flac": "taken"})
    await h.edit_album("album", "Abbey Road", "Let It Be")
    cs = h.waiting_move()
    assert all("/Abbey Road/" in f.dst for f in cs.files)  # unchanged: the new plan conflicts
    job_id = await h.supervisor.decide(cs.id, accept=True, by="ui")
    await h.until(lambda: (j := h.db.job(job_id)) is not None and j.state == "failed")
    job = h.db.job(job_id)
    assert job is not None
    assert "conflicts now" in (job.error or "")
    assert all(p.startswith("/inbox/") for p in h.paths())
    assert h.state() == "waiting_review"


async def test_LIBRARY_MOVE_19_a_partial_move_fails_the_lane(h: ReplanHarness) -> None:
    cs = await waiting(h)
    second = sorted(f.src for f in cs.files if f.kind == "item")[1]
    h.backend.fail_files[second] = "disk full"
    await h.supervisor.decide(cs.id, accept=True, by="ui")
    await h.until(lambda: h.state() in ("failed", "succeeded"))
    assert h.state() == "failed"
    assert "after move" not in h.logs()
    finished = [e.data for e in h.events("lane.finished")]
    assert any(d["state"] == "failed" and "disk full" in str(d["reason"]) for d in finished)
