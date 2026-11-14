"""F-IMPORT-UI in the worker and supervisor: decisions, lookups, timeouts, cancel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppImportDecisions, AppLanes, ImportResolvedError
from rifferr.core.imports import ImportDecision
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.beets_import import BeetsImportStep
from rifferr.steps.review import ReviewStep
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

AFTER = "{id: after, uses: test.per-album}"


def import_step(**params: str) -> str:
    extra = "".join(f", {k}: {v}" for k, v in params.items())
    return f"{{id: import, uses: beets.import, with: {{path: /inbox{extra}}}}}"


IMPORT_AFTER = wf(import_step(), AFTER)

STRONG = {"album": "A", "albumartist": "Artist", "items": [{"title": "a1"}]}
WEAK = {
    "album": "b-folder",
    "_rec": "weak",
    "_candidates": [
        {"id": "MusicBrainz:b1", "distance": 0.31, "fields": {"album": "B", "albumartist": "X"}},
        {"id": "MusicBrainz:b2", "distance": 0.45, "fields": {"album": "B2", "albumartist": "X"}},
    ],
    "items": [{"title": "b1"}],
}


class ImportHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), BeetsImportStep()]),
            self.backend,
            self._send,
        )
        self.imports = AppImportDecisions(self.db)
        self.inbox = tmp_path / "inbox"
        self.inbox.mkdir()

    async def waiting_import(self, source: str = IMPORT_AFTER, *albums: dict[str, Any]) -> None:
        self.backend.add_import_fixture(self.inbox, list(albums) or [STRONG, WEAK])
        self.submit(source.replace("/inbox", str(self.inbox)))
        await self.until(lambda: self.state() == "waiting_review")

    def decision(self) -> ImportDecision:
        (decision,) = self.imports.for_run(RUN)
        return decision

    def lanes(self) -> dict[str, tuple[str, str]]:
        return {k: (lane.label, lane.state) for k, lane in AppLanes(self.db).load(RUN).items()}

    async def job_done(self, job_id: str) -> str:
        await self.until(
            lambda: (j := self.db.job(job_id)) is not None and j.state in ("succeeded", "failed")
        )
        job = self.db.job(job_id)
        assert job is not None
        return job.state

    def album_names(self) -> list[str]:
        return sorted(str(a.fields.get("album")) for a in self.backend.query(Selection()))


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[ImportHarness]:
    harness = ImportHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def test_IMPORT_UI_5_weak_album_waits_in_a_pending_lane(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    assert decision.state == "pending"
    assert decision.path == f"{h.inbox}/b-folder"
    assert [c.id for c in decision.candidates] == ["MusicBrainz:b1", "MusicBrainz:b2"]
    lanes = h.lanes()
    assert lanes[decision.lane_key] == ("b-folder", "waiting")
    assert ("Artist - A", "done") in lanes.values()  # the strong album went on
    assert "processed A" in h.logs()


async def test_IMPORT_UI_6_import_requested_event(h: ImportHarness) -> None:
    await h.waiting_import()
    (event,) = h.events("import.requested")
    assert event.step_id == "import"
    assert event.data["decision_id"] == h.decision().id
    assert event.data["path"] == f"{h.inbox}/b-folder"
    assert event.data["kind"] == "match"


async def test_IMPORT_UI_12_candidate_imports_and_the_lane_goes_on(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    job = await h.supervisor.decide_import(
        decision.id, "candidate", candidate_id="MusicBrainz:b1", by="ui", note="looks right"
    )
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    done = h.decision()
    assert done.state == "applied"
    assert (done.decided_by, done.note, done.action) == ("ui", "looks right", "candidate")
    assert done.album_id is not None
    assert done.decided_at is not None
    (resolved,) = h.events("import.resolved")
    assert resolved.data == {
        "decision_id": decision.id,
        "action": "candidate",
        "album_id": done.album_id,
    }
    lanes = h.lanes()
    assert decision.lane_key not in lanes
    assert lanes[done.album_id] == ("X - B", "done")  # renamed (LANES-14), then finished
    assert "processed B" in h.logs()
    assert h.album_names() == ["A", "B"]


async def test_IMPORT_UI_11_asis_imports_with_current_tags(h: ImportHarness) -> None:
    await h.waiting_import()
    job = await h.supervisor.decide_import(h.decision().id, "asis", by="ui")
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.album_names() == ["A", "b-folder"]
    assert h.decision().state == "applied"


async def test_IMPORT_UI_17_skip_ends_the_lane_skipped(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    job = await h.supervisor.decide_import(decision.id, "skip", by="ui")
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.decision().state == "skipped"
    assert h.lanes()[decision.lane_key] == ("b-folder", "skipped")
    assert h.album_names() == ["A"]
    assert h.events("run.finished")[-1].data == {"status": "succeeded"}


async def test_IMPORT_UI_17_all_lanes_skipped_ends_succeeded(h: ImportHarness) -> None:
    await h.waiting_import(IMPORT_AFTER, WEAK)
    job = await h.supervisor.decide_import(h.decision().id, "skip", by="ui")
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert [state for _, state in h.lanes().values()] == ["skipped"]


async def test_IMPORT_UI_8_decided_once(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    job = await h.supervisor.decide_import(decision.id, "skip", by="ui")
    await h.job_done(job)
    with pytest.raises(ImportResolvedError):
        await h.supervisor.decide_import(decision.id, "asis", by="ui")


async def test_IMPORT_UI_8_rejects_bad_answers(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    with pytest.raises(KeyError):
        await h.supervisor.decide_import("nope", "skip")
    with pytest.raises(ValueError, match="isn't one of"):
        await h.supervisor.decide_import(decision.id, "candidate", candidate_id="MusicBrainz:zz")
    with pytest.raises(ValueError, match="choose a candidate_id"):
        await h.supervisor.decide_import(decision.id, "candidate")
    with pytest.raises(ValueError, match="doesn't answer a match"):
        await h.supervisor.decide_import(decision.id, "merge")
    assert h.decision().state == "pending"


async def test_IMPORT_UI_11_changed_folder_stays_pending(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    h.backend.change_folder(decision.path)
    job = await h.supervisor.decide_import(
        decision.id, "candidate", candidate_id="MusicBrainz:b1", by="ui"
    )
    assert await h.job_done(job) == "failed"
    after = h.decision()
    assert after.state == "pending"
    assert after.error == "the folder changed; look it up again"
    assert h.state() == "waiting_review"
    assert h.album_names() == ["A"]


async def test_IMPORT_UI_13_failure_logs_and_allows_a_retry(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    h.backend.fail_lookup = "MusicBrainz is unreachable"
    job = await h.supervisor.decide_import(
        decision.id, "candidate", candidate_id="MusicBrainz:b1", by="ui"
    )
    assert await h.job_done(job) == "failed"
    assert h.decision().error == "MusicBrainz is unreachable"
    assert any("MusicBrainz is unreachable" in line for line in h.logs())
    h.backend.fail_lookup = None
    job = await h.supervisor.decide_import(decision.id, "asis", by="ui")  # retry, another action
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.decision().error is None


async def test_IMPORT_UI_9_lookup_by_id_adds_candidates(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    h.backend.add_lookup_candidate(
        {"id": "MusicBrainz:b9", "distance": 0.05, "fields": {"album": "B9", "albumartist": "Y"}}
    )
    job = await h.supervisor.lookup_import(decision.id, ids=["MusicBrainz:b9"])
    assert await h.job_done(job) == "succeeded"
    ids = [c.id for c in h.decision().candidates]
    assert ids == ["MusicBrainz:b1", "MusicBrainz:b2", "MusicBrainz:b9"]
    # Searching again finds nothing new and repeats nothing.
    job = await h.supervisor.lookup_import(decision.id, artist="Y", album="B9")
    assert await h.job_done(job) == "succeeded"
    assert [c.id for c in h.decision().candidates] == ids
    assert h.backend.lookup_calls[-1] == (f"{h.inbox}/b-folder", (), "Y", "B9")
    assert h.decision().state == "pending"
    # The added candidate can then be chosen.
    job = await h.supervisor.decide_import(decision.id, "candidate", candidate_id="MusicBrainz:b9")
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.album_names() == ["A", "B9"]


async def test_IMPORT_UI_9_plain_lookup_replaces_and_refreshes(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    h.backend.add_lookup_candidate({"id": "MusicBrainz:b9", "distance": 0.05, "fields": {}})
    job = await h.supervisor.lookup_import(decision.id, ids=["MusicBrainz:b9"])
    await h.job_done(job)
    h.backend.change_folder(decision.path)
    job = await h.supervisor.lookup_import(decision.id)
    assert await h.job_done(job) == "succeeded"
    after = h.decision()
    assert [c.id for c in after.candidates] == ["MusicBrainz:b1", "MusicBrainz:b2"]
    assert after.fingerprint != decision.fingerprint  # a changed folder can be applied again
    job = await h.supervisor.decide_import(decision.id, "asis")
    assert await h.job_done(job) == "succeeded"


async def test_IMPORT_UI_10_timeout_decides_with_on_timeout(h: ImportHarness) -> None:
    await h.waiting_import(wf(import_step(decision_timeout="1h", on_timeout="asis"), AFTER))
    decision = h.decision()
    assert decision.deadline is not None
    assert await h.supervisor.decide_overdue_imports() == []  # not yet
    h.now = decision.deadline + timedelta(seconds=1)
    assert await h.supervisor.decide_overdue_imports() == [decision.id]
    await h.until(lambda: h.state() == "succeeded")
    done = h.decision()
    assert (done.state, done.action, done.decided_by) == ("applied", "asis", "timeout")


async def test_IMPORT_UI_10_no_timeout_waits(h: ImportHarness) -> None:
    await h.waiting_import()
    assert h.decision().deadline is None
    h.now = h.now + timedelta(days=3650)
    assert await h.supervisor.decide_overdue_imports() == []


async def test_IMPORT_UI_14_cancel_cancels_pending_decisions(h: ImportHarness) -> None:
    await h.waiting_import()
    decision = h.decision()
    await h.supervisor.cancel(RUN)
    assert h.state() == "cancelled"
    assert h.decision().state == "cancelled"
    assert h.lanes()[decision.lane_key] == ("b-folder", "cancelled")
    with pytest.raises(ImportResolvedError):
        await h.supervisor.decide_import(decision.id, "skip")


async def test_IMPORT_UI_3_duplicate_choice_keeps_both(h: ImportHarness) -> None:
    h.backend.add_album({"album": "D", "albumartist": "Old", "items": [{"title": "d"}]})
    dup = {"album": "D", "_duplicate": True, "items": [{"title": "d"}]}
    await h.waiting_import(wf(import_step(options="{duplicate_action: ask}"), AFTER), dup)
    decision = h.decision()
    assert decision.kind == "duplicate"
    assert [d.fields["album"] for d in decision.duplicates] == ["D"]
    with pytest.raises(ValueError, match="doesn't answer a duplicate"):
        await h.supervisor.decide_import(decision.id, "asis")
    job = await h.supervisor.decide_import(decision.id, "keep", by="ui")
    assert await h.job_done(job) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert h.album_names() == ["D", "D"]
