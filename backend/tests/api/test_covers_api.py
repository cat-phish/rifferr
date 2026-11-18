"""S-COVERS-SEARCH routes: search streaming, images, cover reviews in the queue and their
decisions (COVERS-SEARCH-19 to -22). Providers are fakes; images come from respx."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import JsonValue

from rifferr.appdb import AppCoverReviews, AppDB
from rifferr.core.covers import (
    CoverCandidate,
    CoverQuery,
    CoverReview,
    CoverSettings,
    RankedCandidate,
)
from rifferr.covers.http import CoverHttp
from rifferr.covers.service import CoverService
from rifferr.engine.events import profile_stream
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run
from tests.covers.images import jpeg, png


class Shop:
    name = "shop"
    hosts = frozenset({"covers.test"})
    needs = False

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.release.set()
        self.queries: list[CoverQuery] = []

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        self.queries.append(query)
        yield CoverCandidate.make(
            "shop",
            image_url="https://covers.test/a.jpg",
            match_confidence=1.0,
            width=1000,
            height=1000,
            thumb_url="https://covers.test/a-thumb.jpg",
        )
        await self.release.wait()
        yield CoverCandidate.make(
            "shop",
            image_url="https://covers.test/b.jpg",
            match_confidence=0.5,
            width=1000,
            height=1000,
        )


@pytest.fixture
def shop(api: Api, tmp_path: Path) -> Shop:
    shop = Shop()
    api.state.covers = CoverService(
        {"shop": shop}, cache_root=tmp_path / "cache", art_root=lambda p: tmp_path / "art" / p
    )
    return shop


@pytest.fixture
def images() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://covers.test/a.jpg").respond(content=jpeg(1000, 1000))
        mock.get("https://covers.test/a-thumb.jpg").respond(content=jpeg(250, 250))
        mock.get("https://covers.test/b.jpg").respond(content=png(1000, 1000))
        mock.get("https://covers.test/tiny.jpg").respond(content=jpeg(100, 100))
        mock.route(host="test").pass_through()
        yield mock


def events(db: AppDB, type_: str) -> list[dict[str, Any]]:
    return [e.data for e in db.events_since(profile_stream("main"), 0) if e.type == type_]


async def until(check: Any, timeout: float = 5) -> None:
    async def poll() -> None:
        while not check():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def review(review_id: str, run_id: str, folder: str | None = None) -> CoverReview:
    return CoverReview(
        id=review_id,
        profile="main",
        run_id=run_id,
        step_id="art",
        album_id="album-1",
        label="Artist - Album",
        query=CoverQuery(artist="Artist", album="Album", folder=folder),
        candidates=[
            RankedCandidate(
                **CoverCandidate.make(
                    "shop", image_url="https://covers.test/tiny.jpg", match_confidence=1
                ).model_dump(),
                score=0.5,
            )
        ],
        created_at=T0,
    )


def seed(db: AppDB, folder: str | None = None) -> str:
    job = waiting_run(db, holds=[])
    AppCoverReviews(db).add(review("v1", job.run, folder))
    return job.run


Q = {"artist": "Artist", "album": "Album"}


async def test_COVERS_SEARCH_19_search_streams_candidates_and_a_ranked_result(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    shop.release.clear()
    r = await post(
        api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["shop"]}
    )
    assert r.status_code == 202
    search_id = r.json()["search_id"]
    db = api.state.db
    await until(lambda: len(events(db, "covers.candidate")) == 1)
    assert events(db, "covers.done") == []  # streamed before the provider finished
    shop.release.set()
    await until(lambda: events(db, "covers.done"))
    (done,) = events(db, "covers.done")
    assert done["search_id"] == search_id
    assert [c["score"] for c in done["ranked"]] == [1.0, 0.5]
    states = [e["status"]["state"] for e in events(db, "covers.provider")]
    assert states == ["searching", "done"]
    assert shop.queries[0].folder is None


async def test_COVERS_SEARCH_19_folder_only_from_the_review_and_cancel(
    api: Api, shop: Shop, images: respx.MockRouter, tmp_path: Path
) -> None:
    seed(api.state.db, folder=str(tmp_path))
    body = {"profile": "main", "query": {**Q, "folder": "/etc"}, "review_id": "v1"}
    shop.release.clear()
    r = await post(api, "/api/covers/search", {**body, "providers": ["shop"]})
    assert r.status_code == 202
    await until(lambda: shop.queries)
    assert shop.queries[0].folder == str(tmp_path)
    search_id = r.json()["search_id"]
    cancel = await api.client.request("DELETE", f"/api/covers/search/{search_id}", json={})
    assert (cancel.status_code, cancel.json()) == (200, {"search_id": search_id})
    await until(lambda: events(api.state.db, "covers.done"))
    assert events(api.state.db, "covers.done")[0]["cancelled"] is True
    bad = await post(api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["x"]})
    assert (bad.status_code, bad.json()["error"]["message"]) == (400, "no cover provider named x")


async def test_COVERS_SEARCH_20_images_only_of_known_candidates_with_a_strict_csp(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    await post(api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["shop"]})
    await until(lambda: events(api.state.db, "covers.done"))
    first = events(api.state.db, "covers.done")[0]["ranked"][0]
    r = await api.client.get(f"/api/covers/image/{first['id']}", params={"profile": "main"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'"
    assert r.content == jpeg(1000, 1000)
    thumb = await api.client.get(
        f"/api/covers/image/{first['id']}", params={"profile": "main", "thumb": "1"}
    )
    assert thumb.content == jpeg(250, 250)
    unknown = await api.client.get("/api/covers/image/abc123", params={"profile": "main"})
    assert unknown.status_code == 404
    stored = await api.client.get(f"/api/covers/image/{'0' * 64}", params={"profile": "main"})
    assert stored.status_code == 404


async def test_COVERS_SEARCH_21_queue_lists_cover_reviews(api: Api, shop: Shop) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "cover"]
    assert (item["id"], item["run_id"]) == ("v1", run_id)
    assert item["cover_review"]["label"] == "Artist - Album"
    assert (await api.client.get("/api/covers/reviews/v1")).json()["album_id"] == "album-1"
    assert (await api.client.get("/api/covers/reviews/nope")).status_code == 404


async def test_COVERS_SEARCH_21_choose_checks_the_image_and_queues_the_job_once(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    seed(api.state.db)
    tiny = review("v1", "x").candidates[0].id
    r = await post(
        api, "/api/covers/reviews/v1/decision", {"decision": "choose", "candidate_id": tiny}
    )
    assert (r.status_code, r.json()["error"]["message"]) == (422, "the image is 100x100: too small")
    await post(api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["shop"]})
    await until(lambda: events(api.state.db, "covers.done"))
    found = events(api.state.db, "covers.done")[0]["ranked"][1]["id"]  # from a new search
    r = await post(
        api, "/api/covers/reviews/v1/decision", {"decision": "choose", "candidate_id": found}
    )
    assert r.status_code == 202, r.text
    body = r.json()
    job = api.state.db.job(body["job_id"])
    assert job is not None
    assert (job.kind, job.payload["sha256"], job.payload["by"]) == (
        "cover_apply",
        body["sha256"],
        "ui",
    )
    assert api.state.covers.store("main").has(body["sha256"])
    again = await post(api, "/api/covers/reviews/v1/decision", {"decision": "skip"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")


async def test_COVERS_SEARCH_21_skip_resolves_at_once(api: Api, shop: Shop) -> None:
    seed(api.state.db)
    r = await post(api, "/api/covers/reviews/v1/decision", {"decision": "skip"})
    assert (r.status_code, r.json()) == (202, {"job_id": None, "sha256": None})
    done = AppCoverReviews(api.state.db).get("v1")
    assert done is not None
    assert (done.state, done.decided_by) == ("skipped", "ui")
    again = await post(api, "/api/covers/reviews/v1/decision", {"decision": "fail"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_resolved")
    missing = await post(api, "/api/covers/reviews/v1/decision", {"decision": "choose"})
    assert missing.status_code == 409


async def test_COVERS_SEARCH_21_local_images_only_from_the_album_folder(
    api: Api, shop: Shop, tmp_path: Path
) -> None:
    seed(api.state.db, folder=str(tmp_path / "album"))
    elsewhere = tmp_path / "other.jpg"
    elsewhere.write_bytes(jpeg(800, 800))
    candidate = CoverCandidate.make("local", path=str(elsewhere), match_confidence=1)
    api.state.covers.cache(CoverSettings()).remember(candidate)
    r = await post(
        api, "/api/covers/reviews/v1/decision", {"decision": "choose", "candidate_id": candidate.id}
    )
    assert (r.status_code, r.json()["error"]["message"]) == (
        400,
        "a local image must be in the album's folder",
    )


def test_COVERS_SEARCH_22_read_only_worker_answers_album_art(tmp_path: Path) -> None:
    from rifferr.backends.fake import FakeBackend
    from rifferr.core.covers import ART_FIELD
    from rifferr.worker.readonly import RequestFailed, album_art

    art = tmp_path / "cover.jpg"
    art.write_bytes(jpeg(600, 600))
    fb = FakeBackend()
    with_art = fb.add_album({"album": "A", "artpath": str(art), ART_FIELD: "a" * 64, "items": []})
    without = fb.add_album({"album": "B", "items": []})
    answer = album_art(fb, {"payload": {"album_id": with_art.id}})
    assert answer == {"sha256": "a" * 64, "path": str(art)}
    assert album_art(fb, {"payload": {"album_id": without.id}}) == {"sha256": None, "path": None}
    with pytest.raises(RequestFailed):
        album_art(fb, {"payload": {"album_id": "nope"}})


async def test_COVERS_SEARCH_22_album_art_route_404s_without_art(api: Api) -> None:
    r = await api.client.get("/api/albums/album-x/art", params={"profile": "main"})
    assert r.status_code == 404
