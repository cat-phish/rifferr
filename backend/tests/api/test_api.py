from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from rifferr.api import sse
from rifferr.api.app import create_api
from rifferr.api.security import read_token_file, secure, token_file_path
from rifferr.appdb import AppDB, migrations, schema_version
from rifferr.engine import Event
from rifferr.profile_store import app_db_path, config_root, create_profile, profile_dir
from rifferr.trust import workflow_hash
from tests.api.conftest import (
    BASE,
    ECHO,
    FAKE_WORKER,
    PORT,
    SLOW,
    Api,
    auth_client,
    parse_sse,
    settings,
    wf,
)

# -- API-1: app DB and migrations ------------------------------------------------------------------


def test_API_1_new_db_is_at_latest_schema_version(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    try:
        assert schema_version(db._conn) == len(migrations())  # pyright: ignore[reportPrivateUsage]
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]  # pyright: ignore[reportPrivateUsage]
        assert mode == "wal"
    finally:
        db.close()


def test_API_1_db_without_schema_version_upgrades_without_loss(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(migrations()[0][2])  # what F-WORKER created, unversioned
    conn.execute(
        "INSERT INTO jobs (id, profile, kind, run_id, state, payload, created_at)"
        " VALUES ('j1', 'main', 'run', 'r1', 'succeeded', '{}', ?)",
        (datetime.now(UTC).isoformat(),),
    )
    conn.execute(
        "INSERT INTO trusted_files (sha256, path_hint, trusted_at, trusted_by)"
        " VALUES ('abc', 'x.yaml', 'now', 'test')"
    )
    conn.commit()
    conn.close()
    db = AppDB(path)
    try:
        assert schema_version(db._conn) == len(migrations())  # pyright: ignore[reportPrivateUsage]
        job = db.job_for_run("r1")
        assert job is not None
        assert job.state == "succeeded"
        assert job.workflow is None
        assert db.is_trusted("abc")
    finally:
        db.close()


def test_API_1_migrating_twice_is_a_no_op(tmp_path: Path) -> None:
    AppDB(tmp_path / "app.db").close()
    db = AppDB(tmp_path / "app.db")
    try:
        rows = db._conn.execute("SELECT version FROM schema_version").fetchall()  # pyright: ignore[reportPrivateUsage]
        assert [r[0] for r in rows] == list(range(1, len(migrations()) + 1))
    finally:
        db.close()


# -- API-2: profiles -------------------------------------------------------------------------------


async def test_API_2_profiles_list_names_without_secret_values(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LASTFM_KEY_VAR", "hunter2-secret-value")
    path = profile_dir(config_root(), "main") / "profile.yaml"
    text = path.read_text().replace("vars: {}", "vars: { music: /srv/music }")
    path.write_text(text.replace("secrets: {}", "secrets: { lastfm: env:LASTFM_KEY_VAR }"))
    create_profile(config_root(), "broken")
    (profile_dir(config_root(), "broken") / "profile.yaml").write_text("backend: [\n")
    r = await api.client.get("/api/profiles")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert by_name["main"]["vars"] == ["music"]
    assert by_name["main"]["secrets"] == ["lastfm"]
    assert by_name["main"]["backend"] == "beets"
    assert by_name["broken"]["error"]
    assert "LASTFM_KEY_VAR" not in r.text
    assert "hunter2" not in r.text


# -- API-3: workflows ------------------------------------------------------------------------------


async def test_API_3_list_and_detail_by_file_stem(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    api.add_workflow("bad", "name: x\nsteps:\n  - {id: a, uses: nope}\n")
    r = await api.client.get("/api/workflows", params={"profile": "main"})
    assert r.status_code == 200
    by_name = {w["name"]: w for w in r.json()}
    assert by_name["echo"]["valid"] is True
    assert by_name["echo"]["sha256"] == workflow_hash(ECHO)
    assert by_name["echo"]["trusted"] is False
    assert by_name["bad"]["valid"] is False
    d = await api.client.get("/api/workflows/echo", params={"profile": "main"})
    assert d.status_code == 200
    assert d.json()["yaml"] == ECHO
    assert d.json()["errors"] == []
    bad = (await api.client.get("/api/workflows/bad", params={"profile": "main"})).json()
    assert bad["valid"] is False
    assert bad["errors"]


async def test_API_3_bad_names_are_errors(api: Api) -> None:
    api.add_workflow("dup", ECHO)
    api.add_workflow("dup", ECHO, suffix=".yml")
    q = {"profile": "main"}
    r = await api.client.get("/api/workflows/..secret", params=q)
    assert (r.status_code, r.json()["error"]["code"]) == (400, "invalid_workflow_name")
    r = await api.client.get("/api/workflows/dup", params=q)
    assert (r.status_code, r.json()["error"]["code"]) == (409, "ambiguous_workflow_name")
    r = await api.client.get("/api/workflows/missing", params=q)
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    r = await api.client.get("/api/workflows", params={"profile": "nobody"})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


# -- API-4: validate + safety report ---------------------------------------------------------------


async def test_API_4_validate_returns_errors_and_safety_report(api: Api) -> None:
    api.add_workflow(
        "sh", wf("{id: fetch, uses: shell, with: {run: [curl, 'https://example.org']}}")
    )
    r = await api.client.post("/api/workflows/sh/validate", params={"profile": "main"}, json={})
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True, body["errors"]
    assert body["trusted"] is False
    kinds = {f["kind"] for f in body["safety"]}
    assert "network_tool" in kinds
    assert all(f["step_id"] == "fetch" for f in body["safety"])
    api.add_workflow("bad", "name: x\nsteps:\n  - {id: a, uses: nope}\n")
    bad = await api.client.post("/api/workflows/bad/validate", params={"profile": "main"}, json={})
    assert bad.json()["valid"] is False
    assert bad.json()["errors"]


# -- API-5: create runs ----------------------------------------------------------------------------


async def test_API_5_post_run_queues_and_returns_201(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    run_id = await api.start("echo")
    done = await api.wait_finished(run_id)
    assert done["state"] == "succeeded"
    assert done["workflow"] == "echo"
    assert done["workflow_hash"] == workflow_hash(ECHO)


async def test_API_5_invalid_workflow_is_422_and_creates_no_run(api: Api) -> None:
    api.add_workflow("bad", "name: x\nsteps:\n  - {id: a, uses: nope}\n")
    r = await api.client.post("/api/runs", json={"profile": "main", "workflow": "bad"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"
    assert r.json()["error"]["details"]
    assert (await api.client.get("/api/runs")).json() == []


async def test_API_5_trust_is_decided_from_the_stored_text(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    api.state.db.trust(workflow_hash(ECHO), "echo.yaml", "test")
    run_id = await api.start("echo")
    assert (await api.wait_finished(run_id))["trusted"] is True


# -- API-6: run list and detail --------------------------------------------------------------------


async def test_API_6_queued_run_shows_pending_steps(api: Api) -> None:
    api.add_workflow("slow", SLOW)
    api.add_workflow("echo", ECHO)
    slow = await api.start("slow")
    queued = await api.start("echo")
    detail = (await api.client.get(f"/api/runs/{queued}")).json()
    assert detail["state"] == "queued"
    assert [(s["id"], s["status"]) for s in detail["steps"]] == [("a", "pending"), ("b", "pending")]
    for run_id in (queued, slow):
        await api.client.post(f"/api/runs/{run_id}/cancel", json={})
    assert (await api.wait_finished(slow))["state"] == "cancelled"


async def test_API_6_finished_run_detail_and_list(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    first = await api.start("echo")
    await api.wait_finished(first)
    second = await api.start("echo")
    detail = await api.wait_finished(second)
    assert [(s["id"], s["status"]) for s in detail["steps"]] == [
        ("a", "succeeded"),
        ("b", "succeeded"),
    ]
    assert detail["started_at"]
    assert detail["finished_at"]
    assert detail["created_at"]
    runs = (await api.client.get("/api/runs", params={"profile": "main"})).json()
    assert [r["id"] for r in runs] == [second, first]  # newest first
    assert len((await api.client.get("/api/runs", params={"limit": 1})).json()) == 1
    assert (await api.client.get("/api/runs", params={"profile": "other"})).json() == []
    r = await api.client.get("/api/runs/nope")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


# -- API-7: cancel ---------------------------------------------------------------------------------


async def test_API_7_cancel_returns_202(api: Api) -> None:
    api.add_workflow("slow", SLOW)
    run_id = await api.start("slow")
    await asyncio.wait_for(_until_running(api, run_id), 15)
    r = await api.client.post(f"/api/runs/{run_id}/cancel", json={})
    assert r.status_code == 202
    assert r.json() == {"id": run_id, "status": "cancel_requested"}
    assert (await api.wait_finished(run_id))["state"] == "cancelled"
    r = await api.client.post("/api/runs/nope/cancel", json={})
    assert r.status_code == 404


async def _until_running(api: Api, run_id: str) -> None:
    while (await api.client.get(f"/api/runs/{run_id}")).json()["state"] != "running":
        await asyncio.sleep(0.05)


# -- API-8: per-run SSE ----------------------------------------------------------------------------


async def test_API_8_replays_then_streams_live_and_closes_after_finished(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    run_id = await api.start("echo")
    # Started right away: part replay, part live. Closes on run.finished.
    r = await api.client.get(f"/api/runs/{run_id}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    msgs = parse_sse(r.text)
    seqs = [int(m["id"]) for m in msgs]
    assert seqs == list(range(1, len(msgs) + 1))  # nothing lost, nothing twice
    assert msgs[-1]["event"] == "run.finished"
    assert all(m["data"]["type"] == m["event"] and m["data"]["seq"] == int(m["id"]) for m in msgs)
    # A finished run: full replay, then close at once.
    again = parse_sse((await api.client.get(f"/api/runs/{run_id}/events")).text)
    assert again == msgs
    tail = await api.client.get(f"/api/runs/{run_id}/events", headers={"last-event-id": "2"})
    assert [int(m["id"]) for m in parse_sse(tail.text)] == seqs[2:]


async def test_API_8_unknown_run_and_bad_last_event_id(api: Api) -> None:
    r = await api.client.get("/api/runs/nope/events")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")
    api.add_workflow("echo", ECHO)
    run_id = await api.start("echo")
    await api.wait_finished(run_id)
    r = await api.client.get(f"/api/runs/{run_id}/events", headers={"last-event-id": "x"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "bad_request")


def _event(run_id: str, seq: int, type: str) -> Event:
    return Event(seq=seq, ts=datetime.now(UTC), run_id=run_id, type=type, data={})


async def test_API_8_live_events_already_replayed_are_dropped(api: Api) -> None:
    db, hub = api.state.db, api.state.hub
    db.append_event(_event("r", 1, "run.started"))
    db.append_event(_event("r", 2, "run.step.started"))
    stream = sse.run_events(api.state, "r", 0)
    first = [await anext(stream), await anext(stream)]
    assert [m.split("\n")[0] for m in first] == ["id: 1", "id: 2"]
    hub.publish(_event("r", 2, "run.step.started"))  # raced with the replay
    hub.publish(_event("r", 3, "run.finished"))
    rest = [m async for m in stream]
    assert [m.split("\n")[0] for m in rest] == ["id: 3"]


async def test_API_8_idle_stream_sends_keepalive_comments(api: Api) -> None:
    stream = sse.run_events(api.state, "idle", 0)
    try:
        assert await asyncio.wait_for(anext(stream), 5) == ": keepalive\n\n"
    finally:
        await stream.aclose()
    assert api.settings.keepalive == 0.2
    from rifferr.api.app import KEEPALIVE

    assert KEEPALIVE == 15.0


# -- API-9: dashboard SSE --------------------------------------------------------------------------


async def test_API_9_dashboard_stream_has_run_level_events_only(api: Api) -> None:
    """Drives the ASGI app by hand: the stream never ends, so httpx can't buffer it."""
    api.add_workflow("echo", ECHO)
    app = secure(api.app, api.settings.security())
    chunks: asyncio.Queue[bytes] = asyncio.Queue()
    disconnect = asyncio.Event()
    started: dict[str, Any] = {}

    async def receive() -> dict[str, Any]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            started.update(message)
        elif message.get("body"):
            await chunks.put(message["body"])

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/events",
        "raw_path": b"/api/events",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", f"127.0.0.1:{PORT}".encode()),
            (b"authorization", f"Bearer {api.settings.token}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }
    task = asyncio.create_task(app(scope, receive, send))  # type: ignore[arg-type]
    try:
        assert await asyncio.wait_for(chunks.get(), 5) == b": connected\n\n"
        assert started["status"] == 200
        run_id = await api.start("echo")
        seen: list[tuple[str, str]] = []
        while not seen or seen[-1][0] != "run.finished":
            text = (await asyncio.wait_for(chunks.get(), 15)).decode()
            for msg in parse_sse(text):
                seen.append((msg["event"], msg["data"]["run_id"]))
        assert seen == [("run.started", run_id), ("run.finished", run_id)]
    finally:
        disconnect.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# -- API-10: OpenAPI -------------------------------------------------------------------------------


async def test_API_10_openapi_describes_every_route(api: Api) -> None:
    r = await api.client.get("/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    paths: dict[str, dict[str, Any]] = doc["paths"]
    for route in ("/api/profiles", "/api/workflows", "/api/runs", "/api/runs/{run_id}/events"):
        assert route in paths
    for path, ops in paths.items():
        for method, op in ops.items():
            ok = {k: v for k, v in op["responses"].items() if k.startswith("2")}
            assert ok, f"{method} {path}"
            for resp in ok.values():
                content = resp.get("content", {})
                assert content, f"{method} {path}: untyped response"
                for media, body in content.items():
                    schema = body["schema"]
                    if media == "text/event-stream":
                        continue
                    assert schema not in ({}, {"title": "Response"}), f"{method} {path}"
            if "requestBody" in op:
                schema = op["requestBody"]["content"]["application/json"]["schema"]
                assert "$ref" in schema, f"{method} {path}: untyped request body"
    assert "ErrorEnvelope" in doc["components"]["schemas"]


# -- API-11: error envelope ------------------------------------------------------------------------


async def test_API_11_framework_errors_use_the_envelope(api: Api) -> None:
    r = await api.client.get("/api/nothing-here")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
    r = await api.client.post("/api/runs", json={"profile": "main"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_request"
    assert r.json()["error"]["details"]


# -- API-12: everything behind the security stack --------------------------------------------------


async def test_API_12_every_route_requires_auth_and_checks_host(api: Api) -> None:
    app = secure(api.app, api.settings.security())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, headers={"origin": BASE}
    ) as anon:
        for path in ("/api/profiles", "/api/runs", "/api/events", "/openapi.json", "/"):
            r = await anon.get(path)
            assert (r.status_code, r.json()["error"]["code"]) == (401, "unauthenticated"), path
        r = await anon.post("/api/runs", json={})
        assert r.status_code == 401
        assert (await anon.get("/healthz")).status_code == 200
    async with auth_client(app, api.settings.token, base="http://evil.example:8765") as c:
        r = await c.get("/api/profiles")
        assert (r.status_code, r.json()["error"]["code"]) == (400, "host_not_allowed")


# -- API-13: frontend ------------------------------------------------------------------------------


async def test_API_13_serves_built_frontend_or_a_hint(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<p>the ui</p>")
    for frontend, expected in ((dist, "the ui"), (None, "pnpm build")):
        s = settings(tmp_path, frontend_dir=frontend)
        app = create_api(s)
        async with (
            app.router.lifespan_context(app),
            auth_client(secure(app, s.security()), s.token) as c,
        ):
            r = await c.get("/")
            assert r.status_code == 200
            assert expected in r.text
            assert (await c.get("/api/profiles")).status_code == 200


# -- API-14: trust ---------------------------------------------------------------------------------


async def test_API_14_trust_exact_hash_or_409(api: Api) -> None:
    path = api.add_workflow("echo", ECHO)
    q = {"profile": "main"}
    r = await api.client.post("/api/workflows/echo/trust", params=q, json={"sha256": "0" * 64})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "hash_mismatch")
    assert not api.state.db.is_trusted(workflow_hash(ECHO))
    sha = (await api.client.get("/api/workflows/echo", params=q)).json()["sha256"]
    r = await api.client.post("/api/workflows/echo/trust", params=q, json={"sha256": sha})
    assert r.status_code == 200
    assert r.json() == {"name": "echo", "sha256": sha, "trusted": True}
    listed = (await api.client.get("/api/workflows", params=q)).json()
    assert listed[0]["trusted"] is True
    path.write_text(ECHO + "# edited\n")
    listed = (await api.client.get("/api/workflows", params=q)).json()
    assert listed[0]["trusted"] is False


# -- API-15: lifecycle -----------------------------------------------------------------------------


async def test_API_15_startup_and_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIFFERR_TEST_HEARTBEAT", "0.2")
    create_profile(config_root(), "main")
    s = settings(tmp_path, db_path=None, write_token=True)
    s.worker_command = lambda p: [
        sys.executable,
        FAKE_WORKER,
        "--profile",
        p,
        "--db",
        str(app_db_path()),
    ]
    (profile_dir(config_root(), "main") / "workflows" / "slow.yaml").write_text(SLOW)
    app = create_api(s)
    async with app.router.lifespan_context(app):
        assert app_db_path().is_file()
        assert read_token_file(PORT) == s.token
        async with auth_client(secure(app, s.security()), s.token) as c:
            r = await c.post("/api/runs", json={"profile": "main", "workflow": "slow"})
            run_id = r.json()["id"]
            state = app.state.rifferr
            while state.db.job_for_run(run_id).state != "running":
                await asyncio.sleep(0.05)
            pid = state.supervisor.worker_pid("main")
            assert pid is not None
    assert not token_file_path(PORT).exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    db = AppDB(app_db_path())
    try:
        job = db.job_for_run(run_id)
        assert job is not None
        assert job.state in ("cancelled", "failed")
    finally:
        db.close()
