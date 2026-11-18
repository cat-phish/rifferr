"""Cover providers, the provider HTTP client, the cache and the search runner, with
replayed replies and no network (S-COVERS-SEARCH COVERS-SEARCH-1, -4 to -6, -11, -13)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
import respx
from pydantic import JsonValue

from rifferr.core.covers import CoverCandidate, CoverQuery, CoverSettings, ProviderSetting
from rifferr.covers.base import CoverProvider
from rifferr.covers.cache import CoverCache
from rifferr.covers.http import CoverHttp, CoverHttpError, host_allowed, user_agent
from rifferr.covers.providers import BUILTINS, CoverArtArchive, Deezer, ITunes, LocalFiles
from rifferr.covers.registry import load_providers
from rifferr.covers.search import DownloadError, ProviderStatus, download, search
from tests.covers.images import jpeg, png

REPLIES = Path(__file__).parent / "replies"
MBID = "11111111-2222-3333-4444-555555555555"
Q = CoverQuery(artist="Boards of Canada", album="Geogaddi", year=2002, mb_albumid=MBID)


def reply(name: str) -> Any:
    return json.loads((REPLIES / name).read_text())


def providers() -> dict[str, CoverProvider]:
    return {p.name: p() for p in BUILTINS}


def http(hosts: set[str], client: httpx.AsyncClient, max_bytes: int = 1000) -> CoverHttp:
    return CoverHttp(client, hosts, max_bytes=max_bytes, timeout=5, contact="me@example.org")


async def collect(
    provider: CoverProvider, query: CoverQuery, settings: Mapping[str, JsonValue] | None = None
) -> list[CoverCandidate]:
    async with httpx.AsyncClient() as client:
        h = CoverHttp(client, provider.hosts, max_bytes=10**6, timeout=5)
        return [c async for c in provider.search(query, settings or {}, h)]


# -- providers (COVERS-SEARCH-1, -4) -----------------------------------------------------


def test_COVERS_SEARCH_1_builtins_load_through_entry_points() -> None:
    found, errors = load_providers()
    assert errors == []
    assert set(found) >= {"coverartarchive", "itunes", "deezer", "local"}
    assert all(isinstance(p, CoverProvider) for p in found.values())


@respx.mock
async def test_COVERS_SEARCH_4_coverartarchive_front_images_by_release() -> None:
    respx.get(f"https://coverartarchive.org/release/{MBID}").respond(json=reply("caa-release.json"))
    (c,) = await collect(CoverArtArchive(), Q)
    assert c.image_url == f"https://coverartarchive.org/release/{MBID}/1111.jpg"
    assert c.thumb_url is not None
    assert c.thumb_url.endswith("1111-500.jpg")
    assert (c.match_confidence, c.width) == (1.0, None)
    assert c.source_url == f"https://musicbrainz.org/release/{MBID}"


@respx.mock
async def test_COVERS_SEARCH_4_coverartarchive_falls_back_to_the_release_group() -> None:
    respx.get(f"https://coverartarchive.org/release/{MBID}").respond(404)
    group = respx.get(
        "https://coverartarchive.org/release-group/9f0d2b6e-5a3c-4c1e-8b7a-2d6e4f1a0c33"
    ).respond(json=reply("caa-release.json"))
    q = Q.model_copy(update={"mb_releasegroupid": "9f0d2b6e-5a3c-4c1e-8b7a-2d6e4f1a0c33"})
    assert len(await collect(CoverArtArchive(), q)) == 1
    assert group.called
    assert await collect(CoverArtArchive(), CoverQuery(artist="a", album="b")) == []


@respx.mock
async def test_COVERS_SEARCH_4_itunes_scores_names_and_asks_for_a_size() -> None:
    route = respx.get("https://itunes.apple.com/search").respond(json=reply("itunes-search.json"))
    found = await collect(ITunes(), Q, {"size": 1400})
    assert route.calls.last.request.url.params["entity"] == "album"
    assert route.calls.last.request.url.params["term"] == "Boards of Canada Geogaddi"
    best, other = found
    assert best.image_url is not None
    assert best.image_url.endswith("/1400x1400bb.jpg")
    assert best.match_confidence == 1.0
    assert other.match_confidence < 0.6
    assert best.title == "Boards of Canada - Geogaddi (2002)"


@respx.mock
async def test_COVERS_SEARCH_4_deezer_knows_its_sizes() -> None:
    route = respx.get("https://api.deezer.com/search/album").respond(
        json=reply("deezer-search.json")
    )
    (c,) = await collect(Deezer(), Q)
    assert route.calls.last.request.url.params["q"] == 'artist:"Boards of Canada" album:"Geogaddi"'
    assert (c.width, c.height, c.match_confidence) == (1000, 1000, 1.0)


async def test_COVERS_SEARCH_4_local_images_in_the_folder_only(tmp_path: Path) -> None:
    (tmp_path / "cover.jpg").write_bytes(jpeg(700, 700))
    (tmp_path / "back.png").write_bytes(png(600, 600))
    (tmp_path / "notes.jpg").write_text("not an image")
    (tmp_path / "Scans").mkdir()
    (tmp_path / "Scans" / "front.jpg").write_bytes(jpeg(900, 900))
    (tmp_path / "link.jpg").symlink_to(tmp_path / "cover.jpg")
    q = CoverQuery(album="x", folder=str(tmp_path))
    found = await collect(LocalFiles(), q)
    assert [(Path(c.path or "").name, c.width) for c in found] == [
        ("cover.jpg", 700),
        ("back.png", 600),
    ]
    ordered = await collect(LocalFiles(), q, {"patterns": ["*.png", "cover.*"]})
    assert [Path(c.path or "").name for c in ordered] == ["back.png", "cover.jpg"]


# -- the HTTP client (COVERS-SEARCH-5) ------------------------------------------------------


def test_COVERS_SEARCH_5_hosts_and_user_agent() -> None:
    assert host_allowed("ia800.us.archive.org", {".archive.org"})
    assert host_allowed("archive.org", {"archive.org"})
    assert not host_allowed("evilarchive.org", {".archive.org"})
    assert user_agent("me@example.org").startswith("Rifferr/")
    assert "me@example.org" in user_agent("me@example.org")


@respx.mock
async def test_COVERS_SEARCH_5_only_https_to_declared_hosts_redirects_too() -> None:
    respx.get("https://coverartarchive.org/a").respond(
        307, headers={"location": "https://ia1.us.archive.org/a.jpg"}
    )
    respx.get("https://ia1.us.archive.org/a.jpg").respond(content=b"img")
    respx.get("https://coverartarchive.org/b").respond(
        302, headers={"location": "https://elsewhere.example/b.jpg"}
    )
    async with httpx.AsyncClient() as client:
        h = http({"coverartarchive.org", ".archive.org"}, client)
        assert await h.get_bytes("https://coverartarchive.org/a") == b"img"
        assert "mailto" not in respx.calls.last.request.headers["user-agent"]
        assert "me@example.org" in respx.calls.last.request.headers["user-agent"]
        with pytest.raises(CoverHttpError, match="isn't one this provider may use"):
            await h.get_bytes("https://coverartarchive.org/b")
        with pytest.raises(CoverHttpError, match="only https"):
            await h.get_bytes("http://coverartarchive.org/a")
        with pytest.raises(CoverHttpError, match="isn't one"):
            await h.get_bytes("https://example.org/x")


@respx.mock
async def test_COVERS_SEARCH_5_bodies_over_the_limit_are_refused() -> None:
    respx.get("https://coverartarchive.org/big").respond(content=b"x" * 2000)
    async with httpx.AsyncClient() as client:
        with pytest.raises(CoverHttpError, match="larger than 1000 bytes"):
            await http({"coverartarchive.org"}, client).get_bytes("https://coverartarchive.org/big")


@respx.mock
async def test_COVERS_SEARCH_5_timeouts_are_reported() -> None:
    respx.get("https://coverartarchive.org/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(CoverHttpError, match="timed out after 5s"):
            await http({"coverartarchive.org"}, client).get_bytes(
                "https://coverartarchive.org/slow"
            )


# -- search runner (COVERS-SEARCH-5, -6, -11) -------------------------------------------------


def all_three() -> CoverSettings:
    return CoverSettings(
        providers=[
            ProviderSetting(name="coverartarchive", weight=3),
            ProviderSetting(name="itunes"),
            ProviderSetting(name="deezer"),
        ]
    )


def cache(tmp_path: Path, now: list[datetime] | None = None) -> CoverCache:
    clock = now or [datetime(2026, 11, 14, tzinfo=UTC)]
    return CoverCache(
        tmp_path / "cache", ttl=timedelta(days=7), max_bytes=10**7, clock=lambda: clock[0]
    )


@respx.mock
async def test_COVERS_SEARCH_5_a_failing_provider_ends_only_its_results(tmp_path: Path) -> None:
    respx.get(f"https://coverartarchive.org/release/{MBID}").respond(500)
    respx.get("https://itunes.apple.com/search").respond(json=reply("itunes-search.json"))
    respx.get("https://api.deezer.com/search/album").mock(side_effect=httpx.ConnectError("down"))
    seen: list[str] = []
    statuses: list[ProviderStatus] = []
    async with httpx.AsyncClient() as client:
        out = await search(
            Q,
            all_three(),
            providers(),
            client,
            on_candidate=lambda c: seen.append(c.provider),
            on_provider=statuses.append,
        )
    assert seen == ["itunes", "itunes"]
    final = {s.name: (s.state, s.message) for s in out.statuses}
    assert final == {
        "coverartarchive": ("failed", "HTTP 500"),
        "itunes": ("done", None),
        "deezer": ("failed", "request failed: ConnectError"),
    }
    assert [s.state for s in statuses if s.name == "itunes"] == ["searching", "done"]
    assert [c.provider for c in out.ranking.ranked] == ["itunes", "itunes"]


@respx.mock
async def test_COVERS_SEARCH_6_providers_run_concurrently() -> None:
    gate = asyncio.Event()

    class Slow:
        name, hosts, needs = "slow", frozenset[str](), False

        async def search(
            self, query: CoverQuery, settings: Any, http: CoverHttp
        ) -> AsyncIterator[CoverCandidate]:
            await gate.wait()
            yield CoverCandidate.make("slow", path="/x.jpg", match_confidence=1)

    class Fast:
        name, hosts, needs = "fast", frozenset[str](), False

        async def search(
            self, query: CoverQuery, settings: Any, http: CoverHttp
        ) -> AsyncIterator[CoverCandidate]:
            yield CoverCandidate.make("fast", path="/y.jpg", match_confidence=1)
            gate.set()  # only reached if both run at once

    s = CoverSettings(providers=[ProviderSetting(name="slow"), ProviderSetting(name="fast")])
    async with httpx.AsyncClient() as client:
        out = await asyncio.wait_for(search(Q, s, {"slow": Slow(), "fast": Fast()}, client), 5)
    assert sorted(c.provider for c in out.candidates) == ["fast", "slow"]


@respx.mock
async def test_COVERS_SEARCH_4_a_provider_needing_a_secret_is_skipped_without_it() -> None:
    class Keyed:
        name, hosts, needs = "keyed", frozenset[str](), True
        got: ClassVar[list[JsonValue]] = []

        async def search(
            self, query: CoverQuery, settings: Any, http: CoverHttp
        ) -> AsyncIterator[CoverCandidate]:
            self.got.append(settings["token"])
            yield CoverCandidate.make("keyed", path="/k.jpg", match_confidence=1)

    keyed = Keyed()
    s = CoverSettings(providers=[ProviderSetting(name="keyed", settings={"secret": "k"})])
    async with httpx.AsyncClient() as client:
        out = await search(Q, s, {"keyed": keyed}, client)
        assert [(x.state, x.message) for x in out.statuses] == [("skipped", "its secret isn't set")]
        out = await search(Q, s, {"keyed": keyed}, client, secret=lambda n: f"value-of-{n}")
    assert keyed.got == ["value-of-k"]
    assert out.statuses[0].state == "done"


@respx.mock
async def test_COVERS_SEARCH_11_results_are_cached_until_they_expire(tmp_path: Path) -> None:
    route = respx.get("https://api.deezer.com/search/album").respond(
        json=reply("deezer-search.json")
    )
    now = [datetime(2026, 11, 14, tzinfo=UTC)]
    c = cache(tmp_path, now)
    s = CoverSettings(providers=[ProviderSetting(name="deezer")])
    async with httpx.AsyncClient() as client:
        first = await search(Q, s, providers(), client, cache=c)
        again = await search(Q, s, providers(), client, cache=c)
        assert route.call_count == 1
        assert again.statuses[0].cached
        assert [x.id for x in again.candidates] == [x.id for x in first.candidates]
        await search(Q, s, providers(), client, cache=c, refresh=True)
        assert route.call_count == 2
        now[0] += timedelta(days=8)
        await search(Q, s, providers(), client, cache=c)
        assert route.call_count == 3
    assert c.candidate(first.candidates[0].id) == first.candidates[0]


def test_COVERS_SEARCH_11_cache_size_is_capped_least_recently_used_first(tmp_path: Path) -> None:
    c = CoverCache(tmp_path, ttl=timedelta(days=1), max_bytes=2500)
    import os

    for i in range(3):
        c.store_image(f"https://x/{i}", b"x" * 1000)
        path = c._image(f"https://x/{i}")  # pyright: ignore[reportPrivateUsage]
        os.utime(path, (1000 + i, 1000 + i))
    c.prune()
    assert c.image("https://x/0") is None
    assert c.image("https://x/2") == b"x" * 1000


# -- downloading (COVERS-SEARCH-13) ----------------------------------------------------------


@respx.mock
async def test_COVERS_SEARCH_13_downloads_are_images_that_pass_the_filters(tmp_path: Path) -> None:
    respx.get("https://e-cdns-images.dzcdn.net/ok.jpg").respond(content=jpeg(1000, 1000))
    respx.get("https://e-cdns-images.dzcdn.net/html.jpg").respond(content=b"<html>")
    respx.get("https://e-cdns-images.dzcdn.net/small.jpg").respond(content=jpeg(200, 200))
    s = CoverSettings()
    c = cache(tmp_path)

    def cand(name: str) -> CoverCandidate:
        return CoverCandidate.make(
            "deezer", image_url=f"https://e-cdns-images.dzcdn.net/{name}", match_confidence=1
        )

    async with httpx.AsyncClient() as client:
        image = await download(cand("ok.jpg"), s, providers(), client, cache=c)
        assert (image.info.width, image.info.mime) == (1000, "image/jpeg")
        await download(cand("ok.jpg"), s, providers(), client, cache=c)
        assert respx.calls.call_count == 1  # cached
        with pytest.raises(DownloadError, match="not a JPEG or PNG"):
            await download(cand("html.jpg"), s, providers(), client, cache=c)
        with pytest.raises(DownloadError, match="200x200: too small"):
            await download(cand("small.jpg"), s, providers(), client, cache=c)
    local = tmp_path / "cover.png"
    local.write_bytes(png(800, 800))
    async with httpx.AsyncClient() as client:
        got = await download(
            CoverCandidate.make("local", path=str(local), match_confidence=1),
            s,
            providers(),
            client,
        )
    assert got.info.mime == "image/png"
