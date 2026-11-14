"""F-TAG-GRID routes: library, check, changes, recent edits, undo, profile events."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rifferr.api.security import secure
from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import PORT, SLOW, Api, parse_sse


async def albums(api: Api, **params: Any) -> dict[str, Any]:
    r = await api.client.get("/api/profiles/main/library", params=params)
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    return body


def year_edit(album: dict[str, Any], new: Any, old: Any = None) -> dict[str, Any]:
    return {
        "target": {"scope": "album", "id": album["id"]},
        "album": album["id"],
        "changes": [{"field": "year", "old": old, "new": new}],
    }


@contextlib.asynccontextmanager
async def stream(api: Api, path: str, last_event_id: str | None = None) -> AsyncIterator[Any]:
    """An endless SSE response, read chunk by chunk (httpx would buffer it forever)."""
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

    headers = [
        (b"host", f"127.0.0.1:{PORT}".encode()),
        (b"authorization", f"Bearer {api.settings.token}".encode()),
    ]
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode()))
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", PORT),
    }
    task = asyncio.create_task(app(scope, receive, send))  # type: ignore[arg-type]

    async def until(event: str) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        while not seen or seen[-1]["event"] != event:
            text = (await asyncio.wait_for(chunks.get(), 15)).decode()
            seen.extend(parse_sse(text))
        return seen

    try:
        yield until
    finally:
        disconnect.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.fixture
def shared_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main fake worker starts with the read-only worker's albums."""
    monkeypatch.setenv("RIFFERR_TEST_MAIN_LIBRARY", "1")


# -- TAG-GRID-2 ---------------------------------------------------------------------------


async def test_TAG_GRID_2_library_route(api: Api) -> None:
    page = await albums(api)
    assert [a["fields"]["album"] for a in page["albums"]] == ["Album 0", "Album 1", "Album 2"]
    assert page["truncated"] is False
    assert "albumtype" in page["album_fields"]
    assert "title" in page["item_fields"]
    page = await albums(api, query="album:1", scope="item", limit=1)
    assert [a["fields"]["album"] for a in page["albums"]] == ["Album 1"]


async def test_TAG_GRID_2_errors(api: Api) -> None:
    r = await api.client.get("/api/profiles/main/library", params={"query": "bare"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "bad_query")
    r = await api.client.get("/api/profiles/main/library", params={"limit": 10001})
    assert r.status_code == 400
    r = await api.client.get("/api/profiles/nope/library")
    assert r.status_code == 404


async def test_TAG_GRID_2_timeout(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    import rifferr.api.routes.library as routes

    # Each library query in the fake read-only worker sleeps through a template call.
    monkeypatch.setattr(routes, "QUERY_TIMEOUT", 0.000001)
    r = await api.client.get("/api/profiles/main/library")
    assert (r.status_code, r.json()["error"]["code"]) == (504, "query_timeout")


async def test_TAG_GRID_2_library_answers_while_a_run_is_active(api: Api) -> None:
    api.add_workflow("slow", SLOW)
    run_id = await api.start("slow")
    page = await albums(api)
    assert len(page["albums"]) == 3
    state = (await api.client.get(f"/api/runs/{run_id}")).json()["state"]
    assert state in ("queued", "running")
    await api.client.post(f"/api/runs/{run_id}/cancel")


# -- TAG-GRID-3 ---------------------------------------------------------------------------


async def test_TAG_GRID_3_profile_grid_columns(api: Api) -> None:
    (profile,) = (await api.client.get("/api/profiles")).json()
    assert profile["grid_columns"] == {"album": None, "item": None}
    yaml = profile_dir(config_root(), "main") / "profile.yaml"
    yaml.write_text(
        yaml.read_text() + "ui:\n  grid_columns:\n    album: [album, year]\n", encoding="utf-8"
    )
    (profile,) = (await api.client.get("/api/profiles")).json()
    assert profile["grid_columns"] == {"album": ["album", "year"], "item": None}


# -- TAG-GRID-7 ---------------------------------------------------------------------------


async def test_TAG_GRID_7_check_route(api: Api) -> None:
    album = (await albums(api))["albums"][0]
    r = await api.client.post(
        "/api/profiles/main/changes/check", json={"targets": [year_edit(album, "2001")]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["problems"] == []
    assert body["targets"][0]["changes"][0]["new"] == 2001
    r = await api.client.post(
        "/api/profiles/main/changes/check", json={"targets": [year_edit(album, "soon")]}
    )
    (problem,) = r.json()["problems"]
    assert problem["field"] == "year"
    r = await api.client.post("/api/profiles/main/changes/check", json={"targets": []})
    assert r.status_code == 400


# -- TAG-GRID-8, -9, -11 -----------------------------------------------------------------


@pytest.mark.usefixtures("shared_library")
async def test_TAG_GRID_8_apply_report_on_the_stream_list_and_undo(api: Api) -> None:
    album = (await albums(api))["albums"][0]
    async with stream(api, "/api/profiles/main/events") as until:
        r = await api.client.post(
            "/api/profiles/main/changes",
            json={"targets": [year_edit(album, "2001")], "rationale": "fix"},
        )
        assert r.status_code == 202, r.text
        created = r.json()
        assert len(created["changesets"]) == 1
        assert created["queued_behind"] == []
        seen = await until("job.finished")
        assert [m["event"] for m in seen] == ["changeset.applied", "job.finished"]
        assert seen[0]["data"]["data"]["changeset_id"] == created["changesets"][0]
        assert seen[-1]["data"]["data"]["job_id"] == created["job_id"]

        (edit,) = (await api.client.get("/api/profiles/main/changes")).json()
        assert edit["changeset"]["id"] == created["changesets"][0]
        assert edit["state"] == "applied"
        assert edit["changeset"]["source"] == {"kind": "manual", "name": "ui"}
        assert edit["changeset"]["rationale"] == "fix"
        assert edit["journal_id"] is not None
        assert edit["undo_journal_id"] is None

        r = await api.client.post(f"/api/profiles/main/journal/{edit['journal_id']}/undo", json={})
        assert r.status_code == 202, r.text
        seen = await until("job.finished")
        # The undo is an inverse ChangeSet: proposed, applied, then the edit is undone.
        assert [m["event"] for m in seen] == [
            "changeset.proposed",
            "changeset.applied",
            "changeset.undone",
            "job.finished",
        ]
        assert seen[-1]["data"]["data"]["state"] == "succeeded"

    (edit,) = (await api.client.get("/api/profiles/main/changes")).json()
    assert edit["state"] == "undone"
    assert edit["undo_journal_id"] is not None
    r = await api.client.post(f"/api/profiles/main/journal/{edit['journal_id']}/undo", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_undoable")
    # Replay after Last-Event-ID (API-8); without it, only new events.
    async with stream(api, "/api/profiles/main/events", last_event_id="2") as until:
        seen = await until("job.finished")
        assert [int(m["id"]) for m in seen] == [3, 4, 5, 6]
    async with stream(api, "/api/profiles/main/events") as until:
        r = await api.client.post(
            "/api/profiles/main/changes", json={"targets": [year_edit(album, "2003", old=2001)]}
        )
        assert r.status_code == 202, r.text
        seen = await until("job.finished")
        assert int(seen[0]["id"]) == 7


async def test_TAG_GRID_8_problems_refuse_the_apply(api: Api) -> None:
    album = (await albums(api))["albums"][0]
    r = await api.client.post(
        "/api/profiles/main/changes", json={"targets": [year_edit(album, "soon")]}
    )
    assert (r.status_code, r.json()["error"]["code"]) == (422, "invalid_changes")
    assert r.json()["error"]["details"][0]["field"] == "year"
    r = await api.client.post(
        "/api/profiles/main/changes",
        json={
            "targets": [
                year_edit(album, "x", old="x")
                | {"changes": [{"field": "albumtype", "old": "ep", "new": "ep"}]}
            ]
        },
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "no_changes")
    assert (await api.client.get("/api/profiles/main/changes")).json() == []


async def test_TAG_GRID_11_undo_of_an_unknown_or_other_profiles_entry(api: Api) -> None:
    r = await api.client.post("/api/profiles/main/journal/nope/undo", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


async def test_TAG_GRID_9_profile_events_errors(api: Api) -> None:
    r = await api.client.get("/api/profiles/nope/events")
    assert r.status_code == 404
    r = await api.client.get("/api/profiles/main/events", headers={"last-event-id": "x"})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "bad_request")


# -- TAG-GRID-13 --------------------------------------------------------------------------


@pytest.mark.usefixtures("shared_library")
async def test_TAG_GRID_13_apply_is_accepted_while_a_run_is_active(api: Api) -> None:
    api.add_workflow("slow", SLOW)
    run_id = await api.start("slow")
    album = (await albums(api))["albums"][0]
    r = await api.client.post(
        "/api/profiles/main/changes", json={"targets": [year_edit(album, "2001")]}
    )
    assert r.status_code == 202, r.text
    assert r.json()["queued_behind"] == [run_id]
    await api.client.post(f"/api/runs/{run_id}/cancel")


async def test_READ_ORDER_1_3_library_query_superseded_by_seq(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A slow preview keeps the read-only worker busy, so the queries wait their turn.
    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "0.3")
    script = "actions:\n  - set: {genre: '$albumartist'}\n"
    busy = asyncio.create_task(
        api.client.post("/api/profiles/main/tagscript/preview", json={"script_text": script})
    )
    await asyncio.sleep(0.3)
    url = "/api/profiles/main/library"
    newest = asyncio.create_task(api.client.get(url, params={"client": "grid", "seq": 5}))
    await asyncio.sleep(0.2)
    late = await api.client.get(url, params={"client": "grid", "seq": 4})
    assert (late.status_code, late.json()["error"]["code"]) == (409, "superseded")
    assert (await newest).status_code == 200
    assert (await busy).status_code == 200
    r = await api.client.get(url, params={"seq": -1})
    assert r.status_code == 400
