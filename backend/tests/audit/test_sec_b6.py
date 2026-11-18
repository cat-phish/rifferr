# ruff: noqa: F811
"""Audit B6 (covers / tagscript / cleanup parsing) findings SEC-121..127.
Each test asserts the SAFE behavior."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from rifferr.core.covers import CoverCandidate, CoverQuery, CoverSettings
from rifferr.covers.http import CoverHttp, CoverHttpError
from rifferr.covers.providers import CoverArtArchive
from rifferr.covers.search import DownloadError, download
from rifferr.profile_store import config_root, create_profile
from rifferr.steps.cleanup_plan import CleanupError, Patterns
from tests.api.conftest import Api, api  # noqa: F401  (fixture)
from tests.covers.images import jpeg, png

# -- SEC-121 ---------------------------------------------------------------------------


class _Drip(httpx.AsyncByteStream):
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(60):  # 3 s in total, each gap far inside the per-read limit
            await asyncio.sleep(0.05)
            yield b"x" * 10


async def test_SEC_121_cover_http_total_time_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_Drip())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        h = CoverHttp(client, {"covers.example"}, max_bytes=1_000_000, timeout=0.2)
        start = time.monotonic()
        with pytest.raises(CoverHttpError):
            await h.get("https://covers.example/a")
        assert time.monotonic() - start < 1.0


# -- SEC-122 ---------------------------------------------------------------------------


async def test_SEC_122_album_art_is_not_read_whole(
    api: Api,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    art = tmp_path / "huge.png"
    header = png(10, 10)
    with art.open("wb") as f:
        f.write(header)
        f.truncate(2 << 30)  # sparse: 2 GiB

    async def ask(*_: Any, **__: Any) -> dict[str, Any]:
        return {"path": str(art), "sha256": "a" * 64}

    monkeypatch.setattr("rifferr.api.routes.covers.ask", ask)
    whole: list[Path] = []

    def spy(self: Path) -> bytes:
        whole.append(self)
        return header

    monkeypatch.setattr(Path, "read_bytes", spy)
    r = await api.client.get("/api/albums/a1/art", params={"profile": "main"})
    assert r.status_code in (200, 413, 502)
    assert whole == [], "the whole art file was read into memory"


# -- SEC-123 ---------------------------------------------------------------------------


async def test_SEC_123_bad_redirect_location_is_cover_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://[bad/x"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        h = CoverHttp(client, {"covers.example"}, max_bytes=1000, timeout=5)
        with pytest.raises(CoverHttpError):
            await h.get("https://covers.example/a")


async def test_SEC_123_bad_candidate_url_is_download_error() -> None:
    cand = CoverCandidate.make(
        "coverartarchive", image_url="https://coverartarchive.org:abc/x.jpg", match_confidence=1
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=jpeg(600, 600)))
    ) as client:
        with pytest.raises(DownloadError):
            await download(cand, CoverSettings(), {"coverartarchive": CoverArtArchive()}, client)


# -- SEC-124 (run in a child so a hang cannot stall the suite) ---------------------------

CHILD = """
import json, sys
from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import run_script
from rifferr.tagscript.models import TagScript
spec = json.loads(sys.argv[1])
fb = FakeBackend.from_fixture({"albums": [{"album": "x", "comments": spec["value"],
    "items": [{"title": "t", "track": 1, "path": spec["path"]}]}]})
(snap,) = fb.query(Selection())
r = run_script(TagScript.model_validate({"actions": [spec["action"]]}), snap, fb)
print("error" if r.error is not None else "ok")
"""


def _run_child(spec: dict[str, Any], limit: float = 5.0) -> str:
    try:
        out = subprocess.run(
            [sys.executable, "-c", CHILD, json.dumps(spec)],
            capture_output=True,
            text=True,
            timeout=limit,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"script action did not return within {limit}s (no time limit)")
    return out.stdout.strip()


def test_SEC_124_replace_regex_is_time_limited() -> None:
    action = {"replace": {"fields": ["comments"], "regex": "(a+)+$", "with": "z"}}
    result = _run_child({"action": action, "value": "a" * 40 + "b", "path": "/m/01.flac"})
    assert result in ("ok", "error")  # must return in time: `regex` may also answer quickly


def test_SEC_124_from_filename_many_placeholders() -> None:
    action = {"from_filename": {"pattern": "$a$b$c$d$e$fZ", "source": "stem"}}
    result = _run_child({"action": action, "value": "v", "path": "/m/" + "A" * 250 + ".flac"})
    assert result in ("ok", "error")  # must return in time; a no-match note is fine


# -- SEC-125 ---------------------------------------------------------------------------


def test_SEC_125_bad_glob_set_is_cleanup_error() -> None:
    with pytest.raises(CleanupError):
        Patterns(["[z-a].txt"])


# -- SEC-126 ---------------------------------------------------------------------------


@respx.mock
async def test_SEC_126_caa_ignores_non_uuid_mbid() -> None:
    route = respx.get(url__startswith="https://coverartarchive.org/").respond(404)
    q = CoverQuery(artist="A", album="B", mb_albumid="not-a-uuid/extra")
    async with httpx.AsyncClient() as client:
        h = CoverHttp(client, CoverArtArchive.hosts, max_bytes=10**6, timeout=5)
        got = [c async for c in CoverArtArchive().search(q, {}, h)]
    assert route.call_count == 0
    assert got == []


# -- SEC-127 ---------------------------------------------------------------------------


async def test_SEC_127_image_route_checks_local_candidate_folder(
    api: Api,
    tmp_path: Path,
) -> None:
    create_profile(config_root(), "other")
    img = tmp_path / "albums" / "A" / "cover.jpg"
    img.parent.mkdir(parents=True)
    img.write_bytes(jpeg(600, 600))
    cand = CoverCandidate.make("local", path=str(img), match_confidence=1)
    api.state.covers.cache(CoverSettings()).remember(cand)
    r = await api.client.get(f"/api/covers/image/{cand.id}", params={"profile": "other"})
    assert r.status_code in (400, 403, 404), r.status_code
