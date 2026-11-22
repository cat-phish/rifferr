"""S-LIBRARY-PRUNE in the worker and supervisor: review, accept, reject, cancel
(LIBRARY-PRUNE-3, -4)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rifferr.appdb import AppLanes, AppPruneDecisions, AppRemovals, PruneResolvedError
from rifferr.core.prune import PruneDecision
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.library_prune import LibraryPruneStep
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_cleanup import CleanupHarness
from tests.worker.test_gates import RUN, serving

AFTER = "{id: after, uses: test.echo, with: {message: after prune}}"


class PruneHarness(CleanupHarness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, LibraryPruneStep()]),
            self.backend,
            self._send,
        )
        self.prunes = AppPruneDecisions(self.db)
        music = tmp_path / "music"
        (music / "Here").mkdir(parents=True)
        (music / "Here" / "01.flac").write_bytes(b"x")
        self.backend.music_dirs = [music]
        self.gone = self.backend.add_album(
            {
                "album": "Gone",
                "path": str(music / "Gone"),
                "items": [{"path": str(music / "Gone" / "01.flac")}],
            }
        ).id
        self.backend.add_album(
            {
                "album": "Here",
                "path": str(music / "Here"),
                "items": [{"path": str(music / "Here" / "01.flac")}],
            }
        )

    async def waiting_prune(self) -> PruneDecision:
        self.submit_trusted(wf("{id: prune, uses: library.prune}", AFTER))
        await self.until(lambda: self.state() == "waiting_review")
        (decision,) = self.prunes.pending(run_id=RUN)
        return decision

    def albums(self) -> set[str]:
        return {str(a.fields["album"]) for a in self.backend.query(Selection())}


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[PruneHarness]:
    harness = PruneHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def test_LIBRARY_PRUNE_3_review_holds_the_run_on_one_decision(h: PruneHarness) -> None:
    decision = await h.waiting_prune()
    assert [a.album_id for a in decision.albums] == [h.gone]
    (lane,) = AppLanes(h.db).load(RUN).values()
    assert (lane.key, lane.state) == (decision.lane_key, "waiting")
    assert h.albums() == {"Gone", "Here"}


async def test_LIBRARY_PRUNE_4_accept_drops_with_a_record_then_the_run_goes_on(
    h: PruneHarness,
) -> None:
    decision = await h.waiting_prune()
    job = await h.supervisor.decide_prune(decision.id, "accept", by="ui", note="ok")
    assert job is not None
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.albums() == {"Here"}
    (record,) = AppRemovals(h.db).for_run(RUN)
    assert (record.state, record.delete_files, record.label) == ("removed", False, "Gone")
    assert record.snapshot is not None
    assert record.snapshot.id == h.gone
    done = h.prunes.get(decision.id)
    assert done is not None
    assert (done.state, done.decided_by, done.result) == (
        "accepted",
        "ui",
        {"removed": [h.gone], "skipped": [], "failed": []},
    )
    assert "after prune" in h.logs()
    with pytest.raises(PruneResolvedError):
        await h.supervisor.decide_prune(decision.id, "reject")


async def test_LIBRARY_PRUNE_3_reject_keeps_everything(h: PruneHarness) -> None:
    decision = await h.waiting_prune()
    assert await h.supervisor.decide_prune(decision.id, "reject", by="ui") is None
    await h.until(lambda: h.state() == "succeeded")
    assert h.albums() == {"Gone", "Here"}
    done = h.prunes.get(decision.id)
    assert done is not None
    assert done.state == "rejected"


async def test_LIBRARY_PRUNE_3_cancel_cancels_the_decision(h: PruneHarness) -> None:
    decision = await h.waiting_prune()
    await h.supervisor.cancel(RUN)
    done = h.prunes.get(decision.id)
    assert done is not None
    assert done.state == "cancelled"
    assert h.albums() == {"Gone", "Here"}
