"""The ``covers.search`` step in the worker, and cover reviews decided through the
supervisor (S-COVERS-SEARCH COVERS-SEARCH-12 to -15, -21)."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import JsonValue

from rifferr.appdb import AppChangeSets, AppCoverReviews, CoverResolvedError
from rifferr.artstore import ArtStore
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.covers import ART_FIELD, CoverCandidate, CoverQuery, sha256_hex
from rifferr.core.selection import Selection
from rifferr.covers.http import CoverHttp
from rifferr.engine import StepTypeRegistry
from rifferr.engine.events import Event
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.covers_search import CoversSearchStep
from rifferr.steps.review import ReviewStep
from rifferr.worker.process import Worker
from tests.covers.images import jpeg
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

AFTER = "{id: after, uses: test.echo, with: {message: 'after covers'}}"


class Shop:
    """A provider with fixed answers per album name; images come from respx."""

    name = "shop"
    hosts = frozenset({"covers.test"})
    needs = False

    def __init__(self) -> None:
        self.answers: dict[str, list[dict[str, Any]]] = {}
        self.queries: list[CoverQuery] = []

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        self.queries.append(query)
        for a in self.answers.get(query.album, []):
            yield CoverCandidate.make(
                "shop", image_url=f"https://covers.test/{a['file']}", **a.get("extra", {})
            )


def step(extra: str = "") -> str:
    return wf(
        "{id: art, uses: covers.search, with: {providers: [shop]" + extra + "}}",
        AFTER,
    )


class CoverHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.shop = Shop()
        self.art = tmp_path / "art"
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry(
                [
                    *ALL,
                    ReviewStep(),
                    CoversSearchStep(
                        providers={"shop": self.shop},
                        cache_root=tmp_path / "cache",
                        art_root=self.art,
                    ),
                ]
            ),
            self.backend,
            self._send,
        )
        self.reviews = AppCoverReviews(self.db)

    def art_of(self, name: str) -> Any:
        (album,) = [a for a in self.backend.query(Selection()) if a.fields["album"] == name]
        return album.fields.get(ART_FIELD)


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[CoverHarness]:
    harness = CoverHarness(tmp_path)
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://covers.test/big.jpg").respond(content=jpeg(1200, 1200))
        mock.get("https://covers.test/small.jpg").respond(content=jpeg(300, 300))
        mock.get("https://covers.test/mid.jpg").respond(content=jpeg(700, 700))
        async with serving(harness):
            yield harness
    harness.db.close()


def answer(file: str, conf: float = 1.0, size: int | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {"match_confidence": conf}
    if size is not None:
        extra |= {"width": size, "height": size}
    return {"file": file, "extra": extra}


async def test_COVERS_SEARCH_13_auto_takes_the_best_and_applies_it(h: CoverHarness) -> None:
    h.album("A", albumartist="Art")
    h.shop.answers["A"] = [answer("mid.jpg", size=700), answer("big.jpg")]
    h.submit(step())
    await h.until(lambda: h.state() == "succeeded")
    sha = sha256_hex(jpeg(1200, 1200))
    assert h.art_of("A") == sha  # the unknown size was probed: 1.0 beats 0.7
    assert ArtStore(h.art).has(sha)
    (query,) = h.shop.queries
    assert (query.artist, query.album, query.year) == ("Art", "A", 2001)
    (proposed,) = h.events("changeset.proposed")
    assert h.events("changeset.applied")
    assert proposed.data["changeset_id"]
    assert any("chose shop 1200x1200" in line for line in h.logs())
    assert h.logs()[-1] == "after covers"


async def test_COVERS_SEARCH_12_albums_with_art_are_skipped(h: CoverHarness) -> None:
    h.album("A", **{ART_FIELD: "a" * 64})
    h.shop.answers["A"] = [answer("big.jpg")]
    h.submit(step())
    await h.until(lambda: h.state() == "succeeded")
    assert h.shop.queries == []
    assert any("has art, skipped" in line for line in h.logs())


async def test_COVERS_SEARCH_12_a_folder_image_becomes_the_albums_art(
    h: CoverHarness, tmp_path: Path
) -> None:
    folder = tmp_path / "music" / "A"
    folder.mkdir(parents=True)
    (folder / "folder.jpg").write_bytes(jpeg(500, 500))
    h.album("A", path=str(folder))
    h.shop.answers["A"] = [answer("big.jpg")]
    h.submit(step())
    await h.until(lambda: h.state() == "succeeded")
    assert h.shop.queries == []
    assert h.art_of("A") == sha256_hex(jpeg(500, 500))
    assert (folder / "folder.jpg").is_file()
    assert any("folder.jpg (500x500), used as the cover" in line for line in h.logs())


async def test_COVERS_SEARCH_12_adopt_local_false_only_skips(
    h: CoverHarness, tmp_path: Path
) -> None:
    folder = tmp_path / "music" / "A"
    folder.mkdir(parents=True)
    (folder / "cover.jpg").write_bytes(jpeg(500, 500))
    h.album("A", path=str(folder))
    h.submit(step(", adopt_local: false"))
    await h.until(lambda: h.state() == "succeeded")
    assert h.art_of("A") is None
    assert any("has art, skipped" in line for line in h.logs())


async def test_COVERS_SEARCH_13_filters_the_real_size_and_follows_on_none(h: CoverHarness) -> None:
    h.album("A")
    h.album("B")
    h.shop.answers["A"] = [answer("small.jpg")]  # unknown size: downloaded, too small
    h.shop.answers["B"] = []
    h.submit(step(", on_none: skip"))
    await h.until(lambda: h.state() == "succeeded")
    assert h.art_of("A") is None
    assert any("300x300: too small" in line for line in h.logs())
    assert any("no cover scored 0.6 or more; skipped" in line for line in h.logs())


async def test_COVERS_SEARCH_14_review_holds_only_that_album(h: CoverHarness) -> None:
    h.album("A")
    h.album("B")
    h.shop.answers["A"] = [answer("big.jpg")]
    h.shop.answers["B"] = [answer("mid.jpg", conf=0.3, size=700)]
    h.submit(step())
    await h.until(lambda: h.state() == "waiting_review")
    (review,) = h.reviews.pending(run_id=RUN)
    assert review.label == "B"
    assert [c.image_url for c in review.candidates] == ["https://covers.test/mid.jpg"]
    assert review.reason == "no cover scored 0.6 or more"
    assert h.art_of("A") is not None
    (requested,) = h.events("cover.requested")
    assert requested.data["review_id"] == review.id
    # A went on to the next step; B waits.
    assert h.logs().count("after covers") == 1


async def test_COVERS_SEARCH_21_choose_sets_the_cover_and_the_lane_goes_on(
    h: CoverHarness,
) -> None:
    h.album("B")
    h.shop.answers["B"] = [answer("mid.jpg", conf=0.3, size=700)]
    h.submit(step())
    await h.until(lambda: h.state() == "waiting_review")
    (review,) = h.reviews.pending(run_id=RUN)
    sha, _ = ArtStore(h.art).put(jpeg(700, 700))
    job = await h.supervisor.decide_cover(review.id, "choose", sha256=sha, by="ui")
    assert job is not None
    await h.until(lambda: h.state() == "succeeded")
    assert h.art_of("B") == sha
    resolved = h.events("cover.resolved")[-1].data
    assert (resolved["decision"], resolved["sha256"]) == ("choose", sha)
    assert h.logs()[-1] == "after covers"
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert (stored.state, stored.chosen, stored.decided_by) == ("chosen", sha, "ui")
    with pytest.raises(CoverResolvedError):
        await h.supervisor.decide_cover(review.id, "skip")


async def test_COVERS_SEARCH_21_skip_and_fail(h: CoverHarness) -> None:
    h.album("B")
    h.album("C")
    h.submit(step(", choose: review"))
    await h.until(lambda: h.state() == "waiting_review")
    b, c = h.reviews.pending(run_id=RUN)
    assert await h.supervisor.decide_cover(b.id, "skip", by="ui") is None
    await h.until(lambda: h.logs().count("after covers") == 1 and h.state() == "waiting_review")
    await h.supervisor.decide_cover(c.id, "fail", by="ui")
    await h.until(lambda: h.state() == "failed")
    assert h.art_of("B") is None
    assert h.logs().count("after covers") == 1


async def test_COVERS_SEARCH_15_apply_review_leaves_the_changeset_for_review(
    h: CoverHarness,
) -> None:
    h.album("A")
    h.shop.answers["A"] = [answer("big.jpg", size=1200)]
    h.submit(
        wf(
            "{id: art, uses: covers.search, apply: review, with: {providers: [shop]}}",
            AFTER,
        )
    )
    await h.until(lambda: h.state() == "waiting_review")
    assert h.art_of("A") is None
    (stored,) = h.changesets.for_run(RUN, "proposed")
    (change,) = stored.changeset.targets[0].changes
    assert change.field == ART_FIELD


async def test_COVERS_SEARCH_14_cancelling_the_run_cancels_its_reviews(h: CoverHarness) -> None:
    h.album("B")
    h.submit(step(", choose: review"))
    await h.until(lambda: h.state() == "waiting_review")
    (review,) = h.reviews.pending(run_id=RUN)
    await h.supervisor.cancel(RUN)
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert stored.state == "cancelled"


async def test_COVERS_SEARCH_1_unknown_providers_fail_the_step(h: CoverHarness) -> None:
    h.album("A")
    h.submit(wf("{id: art, uses: covers.search, with: {providers: [shop, nosuch]}}"))
    await h.until(lambda: h.state() == "failed")
    finished = h.events("run.step.finished")[-1].data
    assert "no cover provider named nosuch" in str(finished.get("error"))


async def test_COVERS_SEARCH_14_a_crashed_run_cancels_its_reviews(
    h: CoverHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An engine error (e.g. "database is locked") after B went to review must not
    # leave B's review pending on a failed run, where it can never be decided.
    original = Worker._record_run_state  # pyright: ignore[reportPrivateUsage]

    def locked(self: Worker, event: Event) -> None:
        if event.type == "run.waiting_review":
            raise sqlite3.OperationalError("database is locked")
        original(self, event)

    monkeypatch.setattr(Worker, "_record_run_state", locked)
    h.album("B")
    h.submit(step(", choose: review"))
    await h.until(lambda: h.state() == "failed")
    (requested,) = h.events("cover.requested")
    assert h.reviews.pending(run_id=RUN) == []
    assert AppChangeSets(h.db).for_run(RUN, "proposed") == []
    stored = h.reviews.get(str(requested.data["review_id"]))
    assert stored is not None
    assert stored.state == "cancelled"


async def test_COVERS_SEARCH_14_start_cancels_reviews_an_ended_run_left_pending(
    h: CoverHarness,
) -> None:
    # As an older version left it: the run failed, its review stayed pending.
    h.album("B")
    h.submit(step(", choose: review"))
    await h.until(lambda: h.state() == "waiting_review")
    (review,) = h.reviews.pending(run_id=RUN)
    changesets = AppChangeSets(h.db)
    changesets.add(
        ChangeSet(
            id="cs-left",
            profile="main",
            run_id=RUN,
            step_id="art",
            source=ChangeSource(kind="manual", name="test"),
            created_at=datetime(2026, 11, 22, tzinfo=UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id="album-1"),
                    changes=[FieldChange(field="label", old=None, new="X")],
                )
            ],
        )
    )
    h.db.cancel_waiting(RUN)  # ends the run without touching its reviews
    assert h.reviews.pending(run_id=RUN) != []
    h.supervisor.start()
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert stored.state == "cancelled"
    assert changesets.for_run(RUN, "proposed") == []
