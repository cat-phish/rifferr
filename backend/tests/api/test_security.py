from __future__ import annotations

import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from rifferr.api.security import (
    EXCHANGE_PATH,
    BindRefusedError,
    SecurityConfig,
    check_bind,
    generate_token,
    read_token_file,
    remove_token_file,
    secure,
    token_file_path,
    write_token_file,
)

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
JSON = {"content-type": "application/json"}
SAME = {"origin": BASE, **JSON}


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Stand-in for the F-API app: echoes the path; sends a CORS header to be stripped."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"access-control-allow-origin", b"*"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": scope["path"].encode()})


def client(config: SecurityConfig, base: str = BASE) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=secure(app, config))  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url=base)


@pytest.fixture
def token() -> str:
    return generate_token()


@pytest.fixture
async def c(token: str) -> AsyncIterator[httpx.AsyncClient]:
    async with client(SecurityConfig(port=PORT, token=token)) as cl:
        yield cl


@pytest.fixture
async def open_c(token: str) -> AsyncIterator[httpx.AsyncClient]:
    async with client(SecurityConfig(port=PORT, token=token, no_auth=True)) as cl:
        yield cl


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# -- SECURITY-1, -9: binding ----------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.2", "[::1]"])
def test_SECURITY_1_loopback_binds_are_allowed(host: str) -> None:
    check_bind(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.5", "myhost.example"])
def test_SECURITY_9_non_loopback_bind_refuses_without_real_auth(host: str) -> None:
    with pytest.raises(BindRefusedError, match=f"binding to {host} requires authentication"):
        check_bind(host)


# -- SECURITY-2: Host allowlist ----------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", f"evil.example:{PORT}", "127.0.0.1:9999", ""])
async def test_SECURITY_2_unknown_host_is_rejected(open_c: httpx.AsyncClient, host: str) -> None:
    r = await open_c.get("/api/runs", headers={"host": host})
    assert r.status_code == 400
    assert r.text != "/api/runs"  # never reached the app


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}"])
async def test_SECURITY_2_loopback_hosts_are_allowed(open_c: httpx.AsyncClient, host: str) -> None:
    r = await open_c.get("/api/runs", headers={"host": host})
    assert r.status_code == 200


async def test_SECURITY_2_configured_extra_host(token: str) -> None:
    config = SecurityConfig(port=PORT, token=token, no_auth=True, extra_hosts=("music.lan",))
    async with client(config, "http://music.lan") as cl:
        assert (await cl.get("/x")).status_code == 200


# -- SECURITY-3: Origin / fetch-site -------------------------------------------------


async def test_SECURITY_3_foreign_origin_is_rejected(open_c: httpx.AsyncClient) -> None:
    r = await open_c.post("/api/runs", json={}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = await open_c.get("/api/runs", headers={"origin": "http://localhost:9999"})
    assert r.status_code == 403


async def test_SECURITY_3_same_origin_mutation_is_allowed(open_c: httpx.AsyncClient) -> None:
    assert (await open_c.post("/api/runs", content=b"{}", headers=SAME)).status_code == 200


async def test_SECURITY_3_mutation_without_origin_needs_fetch_site_or_bearer(
    open_c: httpx.AsyncClient, token: str
) -> None:
    assert (await open_c.post("/api/runs", content=b"{}", headers=JSON)).status_code == 403
    r = await open_c.post(
        "/api/runs", content=b"{}", headers={"sec-fetch-site": "same-origin", **JSON}
    )
    assert r.status_code == 200
    r = await open_c.post(
        "/api/runs", content=b"{}", headers={"sec-fetch-site": "cross-site", **JSON}
    )
    assert r.status_code == 403
    r = await open_c.post("/api/runs", content=b"{}", headers={**bearer(token), **JSON})
    assert r.status_code == 200
    r = await open_c.post("/api/runs", content=b"{}", headers={**bearer("wrong"), **JSON})
    assert r.status_code == 403


async def test_SECURITY_3_reads_without_origin_are_fine(open_c: httpx.AsyncClient) -> None:
    assert (await open_c.get("/api/runs")).status_code == 200


async def test_SECURITY_3_exempt_prefix_skips_origin_and_json(token: str) -> None:
    config = SecurityConfig(port=PORT, token=token, no_auth=True, exempt_prefixes=("/hooks/",))
    async with client(config) as cl:
        r = await cl.post("/hooks/lidarr", content=b"x=1", headers={"origin": "https://x"})
        assert r.status_code == 200


# -- SECURITY-4: JSON only -----------------------------------------------------------


@pytest.mark.parametrize(
    "ctype", ["application/x-www-form-urlencoded", "text/plain", "multipart/form-data", None]
)
async def test_SECURITY_4_mutations_must_be_json(
    open_c: httpx.AsyncClient, ctype: str | None
) -> None:
    headers = {"origin": BASE}
    if ctype:
        headers["content-type"] = ctype
    assert (await open_c.post("/api/runs", content=b"a=1", headers=headers)).status_code == 415


async def test_SECURITY_4_json_with_charset_is_fine(open_c: httpx.AsyncClient) -> None:
    headers = {"origin": BASE, "content-type": "application/json; charset=utf-8"}
    assert (await open_c.put("/api/x", content=b"{}", headers=headers)).status_code == 200


# -- SECURITY-5, -13: headers -----------------------------------------------------------


async def test_SECURITY_5_no_cors_headers(open_c: httpx.AsyncClient) -> None:
    r = await open_c.get("/x")
    assert not [h for h in r.headers if h.lower().startswith("access-control-")]


async def test_SECURITY_13_framing_and_referrer_headers_on_every_response(
    c: httpx.AsyncClient,
) -> None:
    for r in (await c.get("/x"), await c.get("/x", headers={"host": "evil"})):
        assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


# -- SECURITY-6, -7: token ---------------------------------------------------------------


def test_SECURITY_6_tokens_are_long_and_random() -> None:
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 43 for t in tokens)  # 32 bytes, base64url


async def test_SECURITY_6_exchange_sets_strict_httponly_cookie_and_redirects(
    c: httpx.AsyncClient, token: str
) -> None:
    r = await c.get(EXCHANGE_PATH, params={"token": token})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert r.headers["cache-control"] == "no-store"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"rifferr_token_{PORT}={token};")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert (await c.get("/api/runs")).status_code == 200  # cookie now in the jar


async def test_SECURITY_6_bad_token_is_refused(c: httpx.AsyncClient) -> None:
    r = await c.get(EXCHANGE_PATH, params={"token": "nope"})
    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert r.headers["cache-control"] == "no-store"


async def test_SECURITY_7_routes_need_cookie_or_bearer(c: httpx.AsyncClient, token: str) -> None:
    assert (await c.get("/api/runs")).status_code == 401
    assert (await c.get("/api/runs", headers=bearer(token))).status_code == 200
    assert (await c.get("/api/runs", headers=bearer(token + "x"))).status_code == 401
    cookie = {"cookie": f"rifferr_token_{PORT}={token}"}
    assert (await c.get("/api/runs", headers=cookie)).status_code == 200


async def test_SECURITY_7_cookie_for_another_port_does_not_count(
    c: httpx.AsyncClient, token: str
) -> None:
    other = {"cookie": f"rifferr_token_9999={token}"}
    assert (await c.get("/api/runs", headers=other)).status_code == 401


async def test_SECURITY_7_health_check_is_public(c: httpx.AsyncClient) -> None:
    assert (await c.get("/healthz")).status_code == 200


async def test_SECURITY_7_no_auth_keeps_the_other_checks(open_c: httpx.AsyncClient) -> None:
    assert (await open_c.get("/api/runs")).status_code == 200
    assert (await open_c.get("/api/runs", headers={"host": "evil"})).status_code == 400
    r = await open_c.post("/api/runs", content=b"{}", headers={"origin": "https://evil", **JSON})
    assert r.status_code == 403
    assert (
        await open_c.post("/api/runs", content=b"a", headers={"origin": BASE})
    ).status_code == 415


def test_SECURITY_7_tokens_compared_in_constant_time() -> None:
    import inspect

    from rifferr.api import security

    assert "hmac.compare_digest" in inspect.getsource(security.SecurityConfig.token_ok)


# -- SECURITY-8: token file ----------------------------------------------------------------


def test_SECURITY_8_token_file_is_private(isolated_env: dict[str, Path]) -> None:
    path = write_token_file(PORT, "secret-token")
    assert path == isolated_env["XDG_RUNTIME_DIR"] / "rifferr" / f"{PORT}.token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert read_token_file(PORT) == "secret-token"
    remove_token_file(PORT)
    assert read_token_file(PORT) is None


def test_SECURITY_8_token_file_falls_back_to_config_dir(tmp_path: Path) -> None:
    env = {"RIFFERR_CONFIG_DIR": str(tmp_path / "cfg")}
    assert token_file_path(PORT, env) == tmp_path / "cfg" / "run" / f"{PORT}.token"


def test_SECURITY_8_token_never_logged(
    caplog: pytest.LogCaptureFixture, isolated_env: dict[str, Path]
) -> None:
    caplog.set_level("DEBUG")
    token = generate_token()
    write_token_file(PORT, token)
    assert token not in caplog.text
