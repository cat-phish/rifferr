# ruff: noqa: F811, PT018, PT022
"""Audit B5 (web/API surface) findings SEC-41..47. Each test asserts the SAFE behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from rifferr.api.app import ApiSettings
from rifferr.api.security import SecurityConfig, Sessions, normalize_host, secure
from rifferr.cli.serve import startup_lines
from rifferr.core.covers import CoverCandidate
from rifferr.core.imports import ImportCandidate
from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import Api, api  # noqa: F401  (fixture)
from tests.api.test_security import BASE, PORT, app, client

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
        c.cookies.clear()  # only the Bearer header, not the jar's new cookie
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
    # The link `rifferr serve` prints carries a one-time code from the server's
    # sessions, not the local token (see startup_lines).
    config = SecurityConfig(port=PORT, token=TOKEN)
    code = config.sessions.sign_in_code()
    async with client(config) as c:
        first = await c.get(f"/auth/exchange?token={code}")
        assert first.status_code == 303 and "set-cookie" in first.headers
        second = await c.get(f"/auth/exchange?token={code}")
        assert second.status_code == 403
        c.cookies.clear()
        r = await c.get("/api/runs", headers={"authorization": f"Bearer {code}"})
        assert r.status_code == 401
    assert TOKEN not in "".join(
        startup_lines(ApiSettings(port=PORT, token=TOKEN), tty=True, ui_port=PORT)
    )


async def test_SEC_43_sign_in_code_expires() -> None:
    now = [0.0]
    sessions = Sessions(code_ttl=60, clock=lambda: now[0])
    code = sessions.sign_in_code()
    now[0] = 61
    async with client(SecurityConfig(port=PORT, token=TOKEN, sessions=sessions)) as c:
        assert (await c.get(f"/auth/exchange?token={code}")).status_code == 403


async def test_SEC_43_sign_in_link_route_needs_the_bearer_token() -> None:
    async with _client() as c:
        url = "/auth/sign-in-link"
        assert (await c.post(url, json={}, headers={"origin": BASE})).status_code == 401
        r = await c.post(url, json={}, headers={"authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        path = r.json()["path"]
        assert TOKEN not in path
        assert (await c.get(path)).status_code == 303
        session = c.cookies[COOKIE]  # a browser session can't mint codes
        c.cookies.clear()
        r = await c.post(url, json={}, headers={"origin": BASE, "cookie": f"{COOKIE}={session}"})
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
        r = await c.post(
            "/api/x",
            headers={
                "host": "nas.lan",
                "origin": "https://other.lan",
                "content-type": "application/json",
                "authorization": f"Bearer {TOKEN}",
            },
            content=b"{}",
        )
        assert r.status_code == 403
        r = await c.get(f"/auth/exchange?token={TOKEN}", headers={"host": "nas.lan"})
        assert r.status_code == 303 and "; Secure" in r.headers["set-cookie"]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "*",
        "http://nas.lan",
        "nas.lan/x",
        "a@nas.lan",
        "nas.lan:0",
        "nas.lan:x",
        "[::1",
        "-x.lan",
    ],
)
def test_SEC_46_allowed_hosts_are_validated(bad: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        normalize_host(bad)
    assert normalize_host("NAS.lan:8443") == "nas.lan:8443"
    assert normalize_host("[::1]:8765") == "[::1]:8765"


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
