"""Providers that need a key, rate limits and redaction (S-COVERS-SEARCH COVERS-SEARCH-26
to -30), with hand-written replies and no network."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import JsonValue

from rifferr.core.covers import CoverCandidate, CoverQuery, CoverSettings, ProviderSetting
from rifferr.core.profile import Profile
from rifferr.covers.base import CoverProvider
from rifferr.covers.http import CoverHttp, CoverHttpError, Throttle, redact_url
from rifferr.covers.providers import BUILTINS, Discogs, FanartTv
from rifferr.covers.search import ProviderStatus, http_for, search

REPLIES = Path(__file__).parent / "replies"
RGID = "11111111-2222-3333-4444-555555555555"
TOKEN = "s3cret-token-value"
Q = CoverQuery(artist="Boards of Canada", album="Geogaddi", year=2002)


def reply(name: str) -> Any:
    return json.loads((REPLIES / name).read_text())


async def collect(
    provider: CoverProvider, query: CoverQuery, settings: Mapping[str, JsonValue]
) -> list[CoverCandidate]:
    async with httpx.AsyncClient() as client:
        h = CoverHttp(
            client,
            provider.hosts,
            max_bytes=10**6,
            timeout=5,
            secret_params=getattr(provider, "secret_params", ()),
        )
        return [c async for c in provider.search(query, settings, h)]


# -- COVERS-SEARCH-26: rate limits -----------------------------------------------------


async def test_COVERS_SEARCH_26_throttle_spaces_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(round(seconds, 1))

    monkeypatch.setattr("rifferr.covers.http.asyncio.sleep", fake_sleep)
    t = Throttle()
    for _ in range(3):
        await t.wait(1.0)
    assert waits == [1.0, 2.0]  # the 2nd and 3rd request wait their turn


@respx.mock
async def test_COVERS_SEARCH_26_429_ends_the_provider_and_holds_the_next_request() -> None:
    respx.get("https://api.example.org/x").respond(429, headers={"Retry-After": "300"})
    t = Throttle()
    async with httpx.AsyncClient() as client:
        h = CoverHttp(client, {"api.example.org"}, max_bytes=1000, timeout=5, throttle=t)
        with pytest.raises(CoverHttpError, match="rate limited"):
            await h.get_json("https://api.example.org/x")
    held = t.next_at - time.monotonic()
    assert 55 < held <= 60  # Retry-After capped at 60 s


def test_COVERS_SEARCH_26_providers_share_a_throttle_by_name() -> None:
    settings = CoverSettings()
    client = httpx.AsyncClient()
    a, b = http_for(Discogs(), client, settings), http_for(Discogs(), client, settings)
    assert a._throttle is b._throttle  # pyright: ignore[reportPrivateUsage]
    assert a.min_interval == 1.0


# -- COVERS-SEARCH-27: secrets stay out ------------------------------------------------


def test_COVERS_SEARCH_27_redact_url() -> None:
    url = f"https://webservice.fanart.tv/v3/x?api_key={TOKEN}&a=1"
    assert TOKEN not in redact_url(url, {"api_key"})
    assert "a=1" in redact_url(url, {"api_key"})
    assert redact_url(url, ()) == url


@respx.mock
async def test_COVERS_SEARCH_27_fanarttv_key_never_in_candidates_or_errors() -> None:
    route = respx.get(f"https://webservice.fanart.tv/v3/music/albums/{RGID}").respond(
        json=reply("fanarttv-album.json")
    )
    found = await collect(
        FanartTv(), CoverQuery(artist="a", album="b", mb_releasegroupid=RGID), {"token": TOKEN}
    )
    assert route.calls.last.request.url.params["api_key"] == TOKEN
    assert all(TOKEN not in c.model_dump_json() for c in found)
    respx.get(f"https://webservice.fanart.tv/v3/music/albums/{RGID}").respond(500)
    with pytest.raises(CoverHttpError) as err:
        await collect(
            FanartTv(), CoverQuery(artist="a", album="b", mb_releasegroupid=RGID), {"token": TOKEN}
        )
    assert TOKEN not in str(err.value)


# -- COVERS-SEARCH-28: Discogs ---------------------------------------------------------


@respx.mock
async def test_COVERS_SEARCH_28_discogs_release_lookup() -> None:
    route = respx.get("https://api.discogs.com/releases/1234567").respond(
        json=reply("discogs-release.json")
    )
    q = Q.model_copy(update={"discogs_albumid": "1234567"})
    (c,) = await collect(Discogs(), q, {"token": TOKEN})
    assert route.calls.last.request.headers["Authorization"] == f"Discogs token={TOKEN}"
    assert (c.image_url, c.width, c.height) == ("https://i.discogs.com/front.jpg", 1200, 675)
    assert c.match_confidence == 1.0
    assert c.source_url is not None
    assert c.source_url.startswith("https://www.discogs.com/")
    assert c.title == "Various - New Release Compilation NRC 8 (1982)"


@respx.mock
async def test_COVERS_SEARCH_28_discogs_search_by_name_barcode_and_catno() -> None:
    route = respx.get("https://api.discogs.com/database/search").respond(
        json=reply("discogs-search.json")
    )
    q = Q.model_copy(update={"barcode": "5021603064121", "catalognum": "WARP CD 101"})
    found = await collect(Discogs(), q, {"token": TOKEN})
    sent = [dict(call.request.url.params) for call in route.calls]
    assert sent[0] == {
        "type": "release",
        "per_page": "5",
        "release_title": "Geogaddi",
        "artist": "Boards of Canada",
    }
    assert sent[1]["barcode"] == "5021603064121"
    assert sent[2]["catno"] == "WARP CD 101"
    # merged by release id; the spacer image is no cover
    assert [c.image_url for c in found] == [
        "https://i.discogs.com/geogaddi.jpg",
        "https://i.discogs.com/geogaddi-re.jpg",
    ]
    first, reissue = found
    assert first.match_confidence == pytest.approx(1.0)
    assert reissue.match_confidence < first.match_confidence  # "(2)" stripped; year differs
    assert (first.width, first.height, reissue.thumb_url) == (None, None, None)
    assert first.source_url == "https://www.discogs.com/release/111-Boards-Of-Canada-Geogaddi"


@respx.mock
async def test_COVERS_SEARCH_28_discogs_max_results() -> None:
    route = respx.get("https://api.discogs.com/database/search").respond(
        json=reply("discogs-search.json")
    )
    q = Q.model_copy(update={"barcode": "123"})
    found = await collect(Discogs(), q, {"token": TOKEN, "max_results": 1})
    assert len(found) == 1
    assert route.call_count == 1


@respx.mock
async def test_COVERS_SEARCH_28_discogs_falls_back_to_search_without_release_images() -> None:
    respx.get("https://api.discogs.com/releases/99").respond(json={"id": 99, "images": []})
    respx.get("https://api.discogs.com/database/search").respond(json=reply("discogs-search.json"))
    q = Q.model_copy(update={"discogs_albumid": "99"})
    assert len(await collect(Discogs(), q, {"token": TOKEN})) == 2


# -- COVERS-SEARCH-29: fanart.tv -------------------------------------------------------


@respx.mock
async def test_COVERS_SEARCH_29_fanarttv_most_liked_first() -> None:
    respx.get(f"https://webservice.fanart.tv/v3/music/albums/{RGID}").respond(
        json=reply("fanarttv-album.json")
    )
    found = await collect(
        FanartTv(), CoverQuery(artist="a", album="b", mb_releasegroupid=RGID), {"token": TOKEN}
    )
    assert [c.image_url for c in found] == [
        "https://assets.fanart.tv/fanart/music/x/albumcover/many.jpg",
        "https://assets.fanart.tv/fanart/music/x/albumcover/few.jpg",
    ]
    assert all(c.match_confidence == 1.0 and c.width is None for c in found)


@respx.mock(assert_all_called=False)
async def test_COVERS_SEARCH_29_fanarttv_needs_a_release_group() -> None:
    route = respx.get(url__startswith="https://webservice.fanart.tv/")
    assert await collect(FanartTv(), Q, {"token": TOKEN}) == []
    assert not route.called


# -- COVERS-SEARCH-30: query, opt-in, secrets ------------------------------------------


def test_COVERS_SEARCH_30_discogs_albumid_in_the_cache_key() -> None:
    assert Q.key() != Q.model_copy(update={"discogs_albumid": "1"}).key()
    assert Q.key() == CoverQuery(artist="Boards of Canada", album="Geogaddi", year=2002).key()


def test_COVERS_SEARCH_30_not_in_the_default_provider_list() -> None:
    names = {p.name for p in CoverSettings().providers}
    assert {"discogs", "fanarttv"}.isdisjoint(names)
    assert {"discogs", "fanarttv"} <= {p.name for p in BUILTINS}


async def test_COVERS_SEARCH_30_skipped_without_its_secret() -> None:
    settings = CoverSettings(
        providers=[ProviderSetting(name="discogs", settings={"secret": "discogs"})]
    )
    statuses: list[ProviderStatus] = []
    async with httpx.AsyncClient() as client:
        await search(
            Q,
            settings,
            {"discogs": Discogs()},
            client,
            secret=lambda _: None,
            on_provider=statuses.append,
        )
    assert [(s.name, s.state, s.message) for s in statuses] == [
        ("discogs", "skipped", "its secret isn't set")
    ]


def test_COVERS_SEARCH_30_validate_warns_about_a_missing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rifferr.cli.files import cover_secret_warnings

    monkeypatch.delenv("RIFFERR_TEST_DISCOGS", raising=False)
    profile = Profile.model_validate(
        {
            "name": "p",
            "root": "/tmp/rifferr-test-root",
            "secrets": {"discogs": "env:RIFFERR_TEST_DISCOGS"},
            "covers": {
                "providers": [
                    {"name": "discogs", "settings": {"secret": "discogs"}},
                    {"name": "fanarttv"},
                    {"name": "itunes"},
                ]
            },
        }
    )
    warnings = cover_secret_warnings(profile)
    assert len(warnings) == 2
    assert "discogs will be skipped" in warnings[0]
    assert "fanarttv needs a secret" in warnings[1]
    monkeypatch.setenv("RIFFERR_TEST_DISCOGS", TOKEN)
    assert cover_secret_warnings(profile) == [
        "cover provider fanarttv needs a secret: set settings.secret"
    ]
