"""F-COVER-PICKER routes: album cover queries and covers outside a run, the user's own
images, the profile's provider defaults, and art caching (COVER-PICKER-2, -8, -10, -14,
-16 to -18)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
import respx

from rifferr.appdb import AppChangeSets
from rifferr.core.covers import ART_FIELD, CoverCandidate, CoverSettings, rank, sha256_hex
from rifferr.profile_store import config_root, profile_dir
from tests.api import test_covers_api
from tests.api.conftest import Api
from tests.api.test_covers_api import Q, Shop, events, seed, until
from tests.api.test_review_queue import post, wait_job
from tests.covers.images import jpeg, png

pytestmark = pytest.mark.usefixtures("shared_library")
shop = test_covers_api.shop
images = test_covers_api.images


@pytest.fixture
def shared_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main fake worker starts with the read-only worker's albums."""
    monkeypatch.setenv("RIFFERR_TEST_MAIN_LIBRARY", "1")


async def first_album(api: Api) -> dict[str, Any]:
    r = await api.client.get("/api/profiles/main/library")
    assert r.status_code == 200, r.text
    album: dict[str, Any] = r.json()["albums"][0]
    return album


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def upload(api: Api, data: bytes, profile: str = "main") -> Any:
    return await api.client.post(
        "/api/covers/uploads", params={"profile": profile}, json={"data": b64(data)}
    )


# -- COVER-PICKER-8 -------------------------------------------------------------------------


def test_COVER_PICKER_8_ranking_keeps_hidden_candidates_with_their_reason() -> None:
    settings = CoverSettings()
    big = CoverCandidate.make(
        "a", image_url="https://x/1", width=1000, height=1000, match_confidence=1
    )
    small = CoverCandidate.make(
        "a", image_url="https://x/2", width=100, height=100, match_confidence=1
    )
    wide = CoverCandidate.make(
        "a", image_url="https://x/3", width=1000, height=500, match_confidence=1
    )
    unknown = CoverCandidate.make("a", image_url="https://x/4", match_confidence=0.8)
    ranking = rank([big, small, wide, unknown], settings, {"a": 2})
    assert [(c.id, c.score, c.size_score, c.filtered) for c in ranking.ranked] == [
        (big.id, 2.0, 1.0, None),
        (unknown.id, 0.8, 0.5, None),
    ]
    assert {c.id: c.filtered for c in ranking.hidden} == {small.id: "small", wide.id: "aspect"}
    assert ranking.dropped == {"a": {"small": 1, "aspect": 1}}


async def test_COVER_PICKER_8_events_carry_size_score_and_filtered(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    await post(api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["shop"]})
    await until(lambda: events(api.state.db, "covers.done"))
    candidate = events(api.state.db, "covers.candidate")[0]["candidate"]
    assert (candidate["score"], candidate["size_score"], candidate["filtered"]) == (1.0, 1.0, None)
    assert events(api.state.db, "covers.done")[0]["hidden"] == []


# -- COVER-PICKER-2, -14 --------------------------------------------------------------------


async def test_COVER_PICKER_2_album_cover_query(api: Api) -> None:
    album = await first_album(api)
    r = await api.client.get(f"/api/profiles/main/albums/{album['id']}/cover-query")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query"]["album"] == "Album 0"
    assert body["query"]["folder"] is None
    assert body["art_sha256"] is None
    missing = await api.client.get("/api/profiles/main/albums/nope/cover-query")
    assert missing.status_code == 404


async def test_COVER_PICKER_14_search_by_album_takes_the_folder_from_the_library(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    album = await first_album(api)
    body = {"profile": "main", "query": {**Q, "folder": "/etc"}, "providers": ["shop"]}
    r = await post(api, "/api/covers/search", {**body, "album_id": album["id"]})
    assert r.status_code == 202, r.text
    await until(lambda: shop.queries)
    assert shop.queries[0].folder == album["path"]
    both = await post(api, "/api/covers/search", {**body, "album_id": "x", "review_id": "y"})
    assert both.status_code == 400


async def test_COVER_PICKER_14_album_cover_is_an_undoable_manual_changeset(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    album = await first_album(api)
    await post(api, "/api/covers/search", {"profile": "main", "query": Q, "providers": ["shop"]})
    await until(lambda: events(api.state.db, "covers.done"))
    best = events(api.state.db, "covers.done")[0]["ranked"][0]["id"]
    r = await post(api, f"/api/profiles/main/albums/{album['id']}/cover", {"candidate_id": best})
    assert r.status_code == 202, r.text
    queued = r.json()
    assert queued["sha256"] == sha256_hex(jpeg(1000, 1000))
    job = await wait_job(api, queued["job_id"])
    assert job.state == "succeeded", job.error
    stored = AppChangeSets(api.state.db).get(queued["changeset_id"])
    assert stored is not None
    assert stored.state == "applied"
    (change,) = stored.changeset.targets[0].changes
    assert (change.field, change.new, change.old) == (ART_FIELD, queued["sha256"], None)
    assert stored.changeset.source.kind == "manual"
    (edit,) = (await api.client.get("/api/profiles/main/changes")).json()
    assert edit["changeset"]["id"] == queued["changeset_id"]
    assert edit["journal_id"] is not None

    # A cover changed meanwhile: the old one the user saw no longer matches.
    second = events(api.state.db, "covers.done")[0]["ranked"][1]["id"]
    r = await post(
        api,
        f"/api/profiles/main/albums/{album['id']}/cover",
        {"candidate_id": second, "old_sha256": "f" * 64},
    )
    assert r.status_code == 202, r.text
    await wait_job(api, r.json()["job_id"])
    conflicted = AppChangeSets(api.state.db).get(r.json()["changeset_id"])
    assert conflicted is not None
    assert conflicted.state == "conflicted"


async def test_COVER_PICKER_14_refusals(
    api: Api, shop: Shop, images: respx.MockRouter, tmp_path: Path
) -> None:
    album = await first_album(api)
    url = f"/api/profiles/main/albums/{album['id']}/cover"
    unknown = await post(api, url, {"candidate_id": "abc"})
    assert unknown.status_code == 404
    elsewhere = tmp_path / "other.jpg"
    elsewhere.write_bytes(jpeg(800, 800))
    local = CoverCandidate.make("local", path=str(elsewhere), match_confidence=1)
    api.state.covers.cache(CoverSettings()).remember(local)
    r = await post(api, url, {"candidate_id": local.id})
    assert (r.status_code, r.json()["error"]["message"]) == (
        400,
        "a local image must be in the album's folder",
    )
    tiny = CoverCandidate.make("shop", image_url="https://covers.test/tiny.jpg", match_confidence=1)
    api.state.covers.cache(CoverSettings()).remember(tiny)
    r = await post(api, url, {"candidate_id": tiny.id})
    assert (r.status_code, r.json()["error"]["code"]) == (422, "bad_image")


# -- COVER-PICKER-17, -18 -------------------------------------------------------------------


async def test_COVER_PICKER_18_upload_becomes_a_candidate(api: Api, shop: Shop) -> None:
    r = await upload(api, png(1200, 1200))
    assert r.status_code == 201, r.text
    candidate = r.json()["candidate"]
    assert (candidate["provider"], candidate["width"], candidate["height"]) == (
        "upload",
        1200,
        1200,
    )
    assert (candidate["score"], candidate["filtered"]) == (1.0, None)
    image = await api.client.get(f"/api/covers/image/{candidate['id']}", params={"profile": "main"})
    assert (image.status_code, image.content) == (200, png(1200, 1200))
    small = (await upload(api, jpeg(200, 200))).json()["candidate"]
    assert small["filtered"] == "small"  # accepted, shown as hidden


async def test_COVER_PICKER_18_upload_refusals(api: Api, shop: Shop) -> None:
    r = await upload(api, b"GIF89a not an image")
    assert (r.status_code, r.json()["error"]["message"]) == (422, "not a JPEG or PNG image")
    r = await api.client.post(
        "/api/covers/uploads", params={"profile": "main"}, json={"data": "***"}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "bad_image")
    yaml = profile_dir(config_root(), "main") / "profile.yaml"
    yaml.write_text(yaml.read_text() + "covers:\n  max_bytes: 1KB\n")
    r = await upload(api, png(1200, 1200) + b"\0" * 4096)
    assert r.status_code == 413, r.text
    assert (r.status_code, r.json()["error"]["code"]) == (413, "too_large")
    r = await upload(api, png(10, 10), profile="nope")
    assert r.status_code == 404
    r = await api.client.post(
        "/api/covers/uploads",
        params={"profile": "main"},
        content=b64(png(10, 10)),
        headers={"content-type": "text/plain"},
    )
    assert r.status_code == 415  # JSON-only (F-SECURITY)


async def test_COVER_PICKER_17_uploaded_image_can_be_chosen_in_a_review(
    api: Api, shop: Shop
) -> None:
    seed(api.state.db)
    candidate = (await upload(api, jpeg(900, 900))).json()["candidate"]
    r = await post(
        api,
        "/api/covers/reviews/v1/decision",
        {"decision": "choose", "candidate_id": candidate["id"]},
    )
    assert r.status_code == 202, r.text
    assert r.json()["sha256"] == sha256_hex(jpeg(900, 900))


# -- COVER-PICKER-10 ------------------------------------------------------------------------


PROFILE_COVERS = """\
covers:
  # weights the user tuned
  target_size: 1200
  providers:
    - name: itunes
      weight: 2   # keep this comment
      settings:
        size: 600
    - name: shop
"""


async def test_COVER_PICKER_10_save_provider_defaults(api: Api, shop: Shop) -> None:
    yaml = profile_dir(config_root(), "main") / "profile.yaml"
    yaml.write_text(yaml.read_text() + PROFILE_COVERS)
    api.state.covers.providers["itunes"] = shop  # registered under another name
    view = (await api.client.get("/api/profiles/main/covers")).json()
    assert [(p["name"], p["enabled"], p["weight"]) for p in view["providers"]] == [
        ("itunes", True, 2),
        ("shop", True, 1),
    ]
    assert view["registered"] == ["itunes", "shop"]
    body = {
        "providers": [
            {"name": "shop", "enabled": True, "weight": 3.5},
            {"name": "itunes", "enabled": False, "weight": 2},
        ],
        "profile_hash": view["profile_hash"],
    }
    r = await api.client.put("/api/profiles/main/covers/providers", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert [(p["name"], p["enabled"], p["weight"]) for p in saved["providers"]] == [
        ("shop", True, 3.5),
        ("itunes", False, 2),
    ]
    text = yaml.read_text()
    assert "# weights the user tuned" in text
    assert "# keep this comment" in text
    assert "size: 600" in text
    assert "target_size: 1200" in text
    # The same hash again: the file changed since it was read.
    stale = await api.client.put("/api/profiles/main/covers/providers", json=body)
    assert (stale.status_code, stale.json()["error"]["code"]) == (409, "conflict")
    bad = await api.client.put(
        "/api/profiles/main/covers/providers",
        json={
            **body,
            "profile_hash": saved["profile_hash"],
            "providers": [{"name": "nosuch", "enabled": True, "weight": 1}],
        },
    )
    assert bad.status_code == 400
    twice = await api.client.put(
        "/api/profiles/main/covers/providers",
        json={
            **body,
            "profile_hash": saved["profile_hash"],
            "providers": [body["providers"][0], body["providers"][0]],
        },
    )
    assert twice.status_code == 400


async def test_COVER_PICKER_10_saved_weights_rank_the_next_search(
    api: Api, shop: Shop, images: respx.MockRouter
) -> None:
    view = (await api.client.get("/api/profiles/main/covers")).json()
    r = await api.client.put(
        "/api/profiles/main/covers/providers",
        json={
            "providers": [{"name": "shop", "enabled": True, "weight": 2}],
            "profile_hash": view["profile_hash"],
        },
    )
    assert r.status_code == 200, r.text
    await post(api, "/api/covers/search", {"profile": "main", "query": Q})
    await until(lambda: events(api.state.db, "covers.done"))
    assert [c["score"] for c in events(api.state.db, "covers.done")[0]["ranked"]] == [2.0, 1.0]


# -- COVER-PICKER-16 ------------------------------------------------------------------------


async def test_COVER_PICKER_16_album_art_has_an_etag(
    api: Api, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    art = tmp_path / "cover.jpg"
    art.write_bytes(jpeg(600, 600))
    sha = sha256_hex(art.read_bytes())

    async def ask(*_: Any, **__: Any) -> dict[str, Any]:
        return {"path": str(art), "sha256": sha}

    monkeypatch.setattr("rifferr.api.routes.covers.ask", ask)
    r = await api.client.get("/api/albums/a1/art", params={"profile": "main"})
    assert r.status_code == 200
    assert r.headers["etag"] == f'"{sha}"'
    assert r.headers["cache-control"] == "private, max-age=3600"
    again = await api.client.get(
        "/api/albums/a1/art", params={"profile": "main"}, headers={"if-none-match": f'"{sha}"'}
    )
    assert (again.status_code, again.content) == (304, b"")
