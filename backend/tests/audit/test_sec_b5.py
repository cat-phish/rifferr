# ruff: noqa: F811, PT018, PT022
"""Audit B5 (web/API surface) findings SEC-41..47. Each test asserts the SAFE behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from rifferr.api.app import ApiSettings
from rifferr.api.security import SecurityConfig, secure
from rifferr.core.covers import CoverCandidate
from rifferr.core.imports import ImportCandidate
from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import Api, api  # noqa: F401  (fixture)
from tests.api.test_security import PORT, app, client

TOKEN = "t" * 43
COOKIE = f"rifferr_token_{PORT}"


def _client(**kw: Any) -> httpx.AsyncClient:
    return client(SecurityConfig(port=PORT, token=TOKEN, **kw))


async def test_SEC_41_get_with_cross_site_fetch_metadata_is_refused() -> None:
    for no_auth in (False, True):
        async with _client(no_auth=no_auth) as c:
            hdrs = {} if no_auth else {"cookie": f"{COOKIE}={TOKEN}"}
            for site in ("same-site", "cross-site"):
                r = await c.get("/api/runs", headers={**hdrs, "sec-fetch-site": site})
                assert r.status_code == 403, (no_auth, site, r.status_code)
            for site in ("same-origin", "none"):  # controls
                r = await c.get("/api/runs", headers={**hdrs, "sec-fetch-site": site})
                assert r.status_code == 200


async def test_SEC_42_cookie_value_is_not_the_bearer_token() -> None:
    async with _client() as c:
        r = await c.get(f"/auth/exchange?token={TOKEN}")
        assert r.status_code == 303
        value = r.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        assert value != TOKEN
        r = await c.get("/api/runs", headers={"authorization": f"Bearer {value}"})
        assert r.status_code == 401


async def test_SEC_42_cookie_survives_foreign_cookies() -> None:
    async with _client() as c:
        for cookie in (
            f"bad[name]=x; {COOKIE}={TOKEN}",
            f"{COOKIE}=junk; {COOKIE}={TOKEN}",
        ):
            r = await c.get("/api/runs", headers={"cookie": cookie})
            assert r.status_code == 200, cookie


async def test_SEC_43_sign_in_link_works_once() -> None:
    async with _client() as c:
        first = await c.get(f"/auth/exchange?token={TOKEN}")
        assert first.status_code == 303 and "set-cookie" in first.headers
        second = await c.get(f"/auth/exchange?token={TOKEN}")
        assert second.status_code == 403
        r = await c.get("/api/runs", headers={"authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 401


def test_SEC_44_candidate_links_are_http_only() -> None:
    bad = "vscode://file/etc/passwd"
    cover = CoverCandidate.make(
        "itunes", image_url="https://x/y.jpg", match_confidence=0.5, source_url=bad
    )
    assert cover.source_url is None
    imp = ImportCandidate(id="mb:1", source="mb", distance=0.1, url=bad)
    assert imp.url is None


async def test_SEC_45_app_shell_csp_limits_scripts() -> None:
    async with _client() as c:
        r = await c.get("/", headers={"cookie": f"{COOKIE}={TOKEN}"})
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp


async def test_SEC_46_configured_host_is_accepted_and_others_refused() -> None:
    settings = ApiSettings(port=PORT, token=TOKEN, extra_hosts=("nas.lan",))  # type: ignore[call-arg]
    wrapped = secure(app, settings.security())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped), base_url="http://nas.lan"
    ) as c:
        r = await c.post(
            "/api/x",
            headers={
                "host": "nas.lan",
                "origin": "https://nas.lan",
                "content-type": "application/json",
                "authorization": f"Bearer {TOKEN}",
            },
            content=b"{}",
        )
        assert r.status_code == 200
        r = await c.get("/api/x", headers={"host": "other.lan", "authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 400


def test_SEC_46_no_auth_refuses_extra_hosts() -> None:
    from rifferr.cli.main import main

    assert main(["serve", "--no-auth", "--allowed-host", "nas.lan", "--no-browser"]) == 2


@pytest.fixture
async def secret_api(api: Api, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    yield api


async def test_SEC_47_env_backed_profile_var_is_not_echoed(
    api: Api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_SECRET", "s3cr3t-env-value")
    path = profile_dir(config_root(), "main") / "profile.yaml"
    path.write_text(path.read_text().replace("vars: {}", 'vars: { key: "{{ env.TEST_SECRET }}" }'))
    api.add_workflow(
        "v",
        "name: v\nvars:\n  key: {}\nsteps:\n"
        "  - {id: a, uses: test.echo, with: {message: '{{ vars.key }}'}}\n",
    )
    r = await api.client.get("/api/workflows", params={"profile": "main"})
    assert r.status_code == 200
    assert "s3cr3t-env-value" not in r.text
    r = await api.client.get("/api/workflows/v", params={"profile": "main"})
    assert "s3cr3t-env-value" not in r.text
    _ = Path
