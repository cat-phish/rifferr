"""F-TRIGGERS webhooks and trigger routes through the full security stack."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx
import pytest

from rifferr.api.security import secure
from rifferr.profile_store import config_root, profile_dir
from rifferr.trust import trust_file
from tests.api.conftest import BASE, Api

HOOK = (
    "name: h\n"
    "on: {webhook: {name: dl, input: files.0.path, within: '{{ vars.inbox }}'}}\n"
    "steps:\n  - {id: a, uses: test.echo, with: {message: '{{ run.input }}'}}\n"
)


@pytest.fixture
async def hooked(api: Api, tmp_path: Path) -> tuple[Api, Path, str]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pfile = profile_dir(config_root(), "main") / "profile.yaml"
    pfile.write_text(
        pfile.read_text().replace("vars: {}", f"vars: {{inbox: '{inbox}'}}"), encoding="utf-8"
    )
    path = api.add_workflow("h", HOOK)
    trust_file(api.state.db, path, "test")
    assert api.state.triggers is not None
    await api.state.triggers.reconcile()
    r = await api.client.post("/api/profiles/main/webhooks/dl/secret", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url_path"] == "/hooks/main/dl"
    return api, inbox, str(body["secret"])


def caller(api: Api) -> httpx.AsyncClient:
    """Another program: no Origin, no local token."""
    app = secure(api.app, api.settings.security())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


def album(inbox: Path, name: str = "A") -> Path:
    folder = inbox / name
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"x")
    return folder


async def test_TRIGGERS_15_webhook_starts_a_run(hooked: tuple[Api, Path, str]) -> None:
    api, inbox, secret = hooked
    folder = album(inbox)
    async with caller(api) as c:
        r = await c.post(
            "/hooks/main/dl",
            json={"files": [{"path": str(folder / "01.flac")}]},
            headers={"authorization": f"Bearer {secret}"},
        )
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    run = await api.wait_finished(run_id)
    assert (run["trigger"], run["input"]) == ("webhook", str(folder))


async def test_TRIGGERS_18_repeat_call_is_skipped(hooked: tuple[Api, Path, str]) -> None:
    api, inbox, secret = hooked
    folder = album(inbox)
    payload = {"files": [{"path": str(folder)}]}
    headers = {"authorization": f"Bearer {secret}"}
    async with caller(api) as c:
        first = await c.post("/hooks/main/dl", json=payload, headers=headers)
        second = await c.post("/hooks/main/dl", json=payload, headers=headers)
    assert first.status_code == 202
    assert second.status_code == 200
    assert second.json()["skipped"] in ("in_progress", "already_run")
    assert second.json()["run_id"] == first.json()["run_id"]


async def test_TRIGGERS_17_basic_auth_password_is_the_secret(
    hooked: tuple[Api, Path, str],
) -> None:
    api, inbox, secret = hooked
    folder = album(inbox)
    basic = base64.b64encode(f"anyone:{secret}".encode()).decode()
    async with caller(api) as c:
        r = await c.post(
            "/hooks/main/dl",
            json={"files": [{"path": str(folder)}]},
            headers={"authorization": f"Basic {basic}"},
        )
    assert r.status_code == 202, r.text


@pytest.mark.parametrize(
    "auth", [None, "Bearer wrong", "Basic !!!", "Token x", "Bearer ", "Basic Og=="]
)
async def test_TRIGGERS_17_missing_or_wrong_secret_is_401(
    hooked: tuple[Api, Path, str], auth: str | None
) -> None:
    api, _, _ = hooked
    headers = {"authorization": auth} if auth else {}
    async with caller(api) as c:
        r = await c.post("/hooks/main/dl", json={}, headers=headers)
    assert r.status_code == 401
    assert "secret" not in r.text.lower()


async def test_TRIGGERS_17_unknown_webhook_is_401_not_404(hooked: tuple[Api, Path, str]) -> None:
    api, _, secret = hooked
    async with caller(api) as c:
        r = await c.post("/hooks/main/nope", json={}, headers={"authorization": f"Bearer {secret}"})
    assert r.status_code == 401


async def test_TRIGGERS_17_secret_in_query_string_is_refused(
    hooked: tuple[Api, Path, str],
) -> None:
    api, _, secret = hooked
    async with caller(api) as c:
        r = await c.post(f"/hooks/main/dl?token={secret}", json={})
    assert r.status_code == 400


async def test_TRIGGERS_17_secret_is_never_logged(
    hooked: tuple[Api, Path, str], caplog: pytest.LogCaptureFixture
) -> None:
    api, inbox, secret = hooked
    caplog.set_level(logging.DEBUG)
    folder = album(inbox)
    async with caller(api) as c:
        await c.post(
            "/hooks/main/dl",
            json={"files": [{"path": str(folder)}]},
            headers={"authorization": f"Bearer {secret}"},
        )
        await c.post("/hooks/main/dl", json={}, headers={"authorization": "Bearer wrong-one"})
    assert secret not in caplog.text
    assert "wrong-one" not in caplog.text
    rows = api.state.db._conn.execute("SELECT * FROM webhook_secrets").fetchall()  # pyright: ignore[reportPrivateUsage]
    assert all(secret not in str(tuple(r)) for r in rows)


async def test_TRIGGERS_17_host_allowlist_still_applies(hooked: tuple[Api, Path, str]) -> None:
    api, _, secret = hooked
    async with caller(api) as c:
        r = await c.post(
            "/hooks/main/dl",
            json={},
            headers={"authorization": f"Bearer {secret}", "host": "evil.example"},
        )
    assert r.status_code in (400, 403, 421)


async def test_TRIGGERS_15_body_must_be_json_and_small(hooked: tuple[Api, Path, str]) -> None:
    api, _, secret = hooked
    auth = {"authorization": f"Bearer {secret}"}
    async with caller(api) as c:
        form = await c.post("/hooks/main/dl", content=b"a=b", headers=auth)
        big = await c.post(
            "/hooks/main/dl",
            content=b"[" + b"0," * (600 * 1024) + b"0]",
            headers={**auth, "content-type": "application/json"},
        )
        bad = await c.post(
            "/hooks/main/dl", content=b"{", headers={**auth, "content-type": "application/json"}
        )
    assert form.status_code == 415
    assert big.status_code == 413
    assert bad.status_code == 400


async def test_TRIGGERS_16_input_outside_within_is_422(
    hooked: tuple[Api, Path, str], tmp_path: Path
) -> None:
    api, _, secret = hooked
    async with caller(api) as c:
        r = await c.post(
            "/hooks/main/dl",
            json={"files": [{"path": str(tmp_path)}]},
            headers={"authorization": f"Bearer {secret}"},
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_input"


async def test_TRIGGERS_16_test_event_is_ignored(hooked: tuple[Api, Path, str]) -> None:
    api, _, secret = hooked
    async with caller(api) as c:
        r = await c.post(
            "/hooks/main/dl",
            json={"eventType": "Test"},
            headers={"authorization": f"Bearer {secret}"},
        )
    assert r.status_code == 200
    assert r.json() == {"ignored": "no_input"}


async def test_TRIGGERS_4_trigger_listing(hooked: tuple[Api, Path, str]) -> None:
    api, _, _ = hooked
    r = await api.client.get("/api/profiles/main/triggers")
    assert r.status_code == 200
    (info,) = r.json()
    assert (info["workflow"], info["kind"], info["state"]) == ("h", "webhook", "armed")
    assert (info["webhook_name"], info["has_secret"]) == ("dl", True)


async def test_TRIGGERS_7_manual_run_with_input(hooked: tuple[Api, Path, str]) -> None:
    api, inbox, secret = hooked
    folder = album(inbox)
    r = await api.client.post(
        "/api/runs", json={"profile": "main", "workflow": "h", "input": str(folder)}
    )
    assert r.status_code == 201, r.text
    run = await api.wait_finished(r.json()["id"])
    assert (run["trigger"], run["input"]) == ("manual", str(folder))
    async with caller(api) as c:  # TRIGGERS-21: the manual run's fingerprint dedups
        hook = await c.post(
            "/hooks/main/dl",
            json={"files": [{"path": str(folder)}]},
            headers={"authorization": f"Bearer {secret}"},
        )
    assert hook.json().get("skipped") == "already_run"


async def test_TRIGGERS_7_manual_input_must_be_absolute(api: Api) -> None:
    api.add_workflow("e", "name: e\nsteps:\n  - {id: a, uses: test.echo, with: {message: x}}\n")
    r = await api.client.post(
        "/api/runs", json={"profile": "main", "workflow": "e", "input": "rel/path"}
    )
    assert r.status_code == 400
