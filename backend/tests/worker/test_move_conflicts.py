"""Move conflicts in the worker: the album waits in the review queue and is retried or
failed through the supervisor (S-LIBRARY-MOVE LIBRARY-MOVE-5a)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppMoveConflicts, MoveResolvedError
from rifferr.backends.base import RUN_MARKER
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.library_move import LibraryMoveStep
from rifferr.steps.review import ReviewStep
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

SRC = "/inbox/Abbey Road"
TAKEN = "/music/The Beatles/Abbey Road/1 Come Together.flac"
MOVE = wf(
    "{id: into, uses: library.move, apply: auto,"
    " with: {directory: /music, stamp: {field: date_added}}}",
    "{id: after, uses: test.echo, with: {message: 'after move'}}",
)


class MoveHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), LibraryMoveStep()]),
            self.backend,
            self._send,
        )
        self.moves = AppMoveConflicts(self.db)

    def beatles(self) -> None:
        album = self.backend.add_album(
            {
                "albumartist": "The Beatles",
                "album": "Abbey Road",
                RUN_MARKER: RUN,
                "path": SRC,
                "items": [{"title": "Come Together", "track": 1}],
            }
        )
        self.backend.album_files(album.id)
        self.backend.add_files({TAKEN: "someone else's file"})

    def path(self) -> str:
        [album] = self.backend.query(Selection())
        return album.items[0].path


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[MoveHarness]:
    harness = MoveHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def waiting(h: MoveHarness) -> str:
    h.beatles()
    h.submit(MOVE)
    await h.until(lambda: h.state() == "waiting_review")
    (conflict,) = h.moves.pending(run_id=RUN)
    assert "already exists" in conflict.conflicts[0]
    assert h.events("move.requested")
    assert "after move" not in h.logs()
    return conflict.id


async def test_LIBRARY_MOVE_5a_retry_after_fixing_moves_and_goes_on(h: MoveHarness) -> None:
    conflict_id = await waiting(h)
    del h.backend.fs[TAKEN]
    job = await h.supervisor.decide_move(conflict_id, "retry", by="ui")
    assert job is not None
    await h.until(lambda: h.state() == "succeeded")
    assert h.path() == TAKEN
    assert h.logs()[-1] == "after move"
    stored = h.moves.get(conflict_id)
    assert stored is not None
    assert (stored.state, stored.decided_by) == ("moved", "ui")
    assert stored.changeset_id is not None
    (applied,) = h.changesets.for_run(RUN, "applied")
    assert applied.changeset.id == stored.changeset_id
    [album] = h.backend.query(Selection())
    assert album.fields.get("date_added")  # the stamp travels with the retry
    resolved = h.events("move.resolved")[-1].data
    assert (resolved["decision"], resolved["changeset_id"]) == ("retry", stored.changeset_id)
    with pytest.raises(MoveResolvedError):
        await h.supervisor.decide_move(conflict_id, "fail")


async def test_LIBRARY_MOVE_5a_retry_still_conflicted_keeps_waiting(h: MoveHarness) -> None:
    conflict_id = await waiting(h)
    job_id = await h.supervisor.decide_move(conflict_id, "retry", by="ui")
    assert job_id is not None

    def finished() -> bool:
        job = h.db.job(job_id)
        return job is not None and job.state == "failed"

    await h.until(finished)
    stored = h.moves.get(conflict_id)
    assert stored is not None
    assert (stored.state, stored.attempts) == ("pending", 1)
    assert h.state() == "waiting_review"
    assert h.path().startswith(SRC)
    # decidable again: the claim was released
    assert await h.supervisor.decide_move(conflict_id, "fail", by="ui") is None


async def test_LIBRARY_MOVE_5a_fail_fails_the_lane(h: MoveHarness) -> None:
    conflict_id = await waiting(h)
    assert await h.supervisor.decide_move(conflict_id, "fail", by="ui") is None
    await h.until(lambda: h.state() == "failed")
    assert h.path().startswith(SRC)
    assert "after move" not in h.logs()
    stored = h.moves.get(conflict_id)
    assert stored is not None
    assert stored.state == "failed"


async def test_LIBRARY_MOVE_5a_cancelling_the_run_cancels_its_conflicts(h: MoveHarness) -> None:
    conflict_id = await waiting(h)
    await h.supervisor.cancel(RUN)
    stored = h.moves.get(conflict_id)
    assert stored is not None
    assert stored.state == "cancelled"


async def test_LIBRARY_MOVE_15_undo_reports_changed_files_and_force_undoes_them(
    h: MoveHarness,
) -> None:
    h.beatles()
    del h.backend.fs[TAKEN]
    h.submit(MOVE)
    await h.until(lambda: h.state() == "succeeded")
    assert h.path() == TAKEN
    h.backend.fs[TAKEN] = b"retagged in the library"

    async def finished(job_id: str) -> Any:
        await h.until(
            lambda: (
                (j := h.db.job(job_id)) is not None and j.state != "queued" and j.state != "running"
            )
        )
        return h.db.job(job_id)

    refused = await finished(await h.supervisor.undo(run_id=RUN, by="ui"))
    assert refused.state == "failed"
    assert refused.result["changed_files"] == [TAKEN]
    assert h.path() == TAKEN
    done = await finished(await h.supervisor.undo(run_id=RUN, by="ui", force=[TAKEN]))
    assert done.state == "succeeded", done.result
    assert h.path().startswith(SRC)
    assert h.backend.fs[h.path()] == b"retagged in the library"
