"""F-IMPORT-UI routes: import decisions in the queue, detail, decision and lookup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from rifferr.api.state import on_all_stream
from rifferr.appdb import AppDB, AppImportDecisions
from rifferr.core.imports import FolderItem, ImportCandidate, ImportDecision
from rifferr.engine import Event
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run


def decision(decision_id: str, run_id: str, *, minutes: int = 0) -> ImportDecision:
    return ImportDecision(
        id=decision_id,
        profile="main",
        run_id=run_id,
        step_id="import",
        kind="match",
        path="/inbox/Band - Live",
        fingerprint="fp",
        items=[FolderItem(path="/inbox/Band - Live/01.flac", tags={"title": "One"})],
        candidates=[
            ImportCandidate(id="MusicBrainz:c1", source="MusicBrainz", album="Live", distance=0.2)
        ],
        created_at=T0 + timedelta(minutes=minutes),
    )


def seed(db: AppDB) -> str:
    job = waiting_run(db, holds=[])
    AppImportDecisions(db).add(decision("d1", job.run, minutes=1))
    return job.run


async def test_IMPORT_UI_7_queue_lists_pending_import_decisions(api: Api) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "import"]
    assert item["id"] == "d1"
    assert item["run_id"] == run_id
    assert item["step_id"] == "import"
    assert item["import_decision"]["candidates"][0]["id"] == "MusicBrainz:c1"
    store = AppImportDecisions(api.state.db)
    done = store.get("d1")
    assert done is not None
    done.state = "skipped"
    store.release(done)
    items = (await api.client.get("/api/review")).json()
    assert [i for i in items if i["kind"] == "import"] == []


async def test_IMPORT_UI_7_get_returns_the_full_decision(api: Api) -> None:
    seed(api.state.db)
    r = await api.client.get("/api/imports/d1")
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/inbox/Band - Live"
    assert body["items"][0]["tags"] == {"title": "One"}
    assert body["state"] == "pending"
    assert (await api.client.get("/api/imports/nope")).status_code == 404


async def test_IMPORT_UI_8_decision_queues_a_job_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/imports/d1/decision", {"action": "skip", "note": "not mine"})
    assert r.status_code == 202
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert job.kind == "import_decision"
    assert job.payload["by"] == "ui"
    # Claimed by that job: a second answer meanwhile is refused.
    again = await post(api, "/api/imports/d1/decision", {"action": "asis"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "busy"
    store = AppImportDecisions(api.state.db)
    done = store.get("d1")
    assert done is not None
    done.state = "skipped"
    store.release(done)
    again = await post(api, "/api/imports/d1/decision", {"action": "asis"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "already_resolved"


async def test_IMPORT_UI_8_bad_answers(api: Api) -> None:
    seed(api.state.db)
    assert (await post(api, "/api/imports/nope/decision", {"action": "skip"})).status_code == 404
    r = await post(api, "/api/imports/d1/decision", {"action": "candidate"})
    assert r.status_code == 400
    r = await post(
        api, "/api/imports/d1/decision", {"action": "candidate", "candidate_id": "MusicBrainz:x"}
    )
    assert r.status_code == 400
    assert (await post(api, "/api/imports/d1/decision", {"action": "merge"})).status_code == 400
    assert (await post(api, "/api/imports/d1/decision", {"action": "burn"})).status_code == 400


async def test_IMPORT_UI_9_lookup_queues_a_job(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/imports/d1/lookup", {"ids": [" MusicBrainz:c9 "], "artist": " "})
    assert r.status_code == 202
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert job.kind == "import_lookup"
    assert job.payload["ids"] == ["MusicBrainz:c9"]
    assert job.payload["artist"] is None


async def test_IMPORT_UI_9_lookup_limits(api: Api) -> None:
    seed(api.state.db)
    too_many = {"ids": [f"id{i}" for i in range(11)]}
    assert (await post(api, "/api/imports/d1/lookup", too_many)).status_code == 400
    assert (await post(api, "/api/imports/d1/lookup", {"ids": ["x" * 201]})).status_code == 400
    assert (await post(api, "/api/imports/d1/lookup", {"album": "x" * 201})).status_code == 400
    assert (await post(api, "/api/imports/nope/lookup", {})).status_code == 404


def test_IMPORT_UI_6_import_events_go_on_the_all_stream() -> None:
    for type in ("import.requested", "import.resolved"):
        event = Event(seq=1, ts=datetime.now(UTC), run_id="r", type=type, step_id=None, data={})
        assert on_all_stream(event)
