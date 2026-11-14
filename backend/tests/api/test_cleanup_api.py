"""S-FILES-CLEANUP routes: cleanup decisions in the queue, detail and decision
(FILES-CLEANUP-14)."""

from __future__ import annotations

from datetime import timedelta

from rifferr.appdb import AppCleanupDecisions, AppDB
from rifferr.core.files import CleanupDecision, CleanupEntry, CleanupOptions
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run


def decision(decision_id: str, run_id: str) -> CleanupDecision:
    return CleanupDecision(
        id=decision_id,
        profile="main",
        run_id=run_id,
        step_id="clean",
        path="/inbox/Band - Live",
        entries=[
            CleanupEntry(path="info.nfo", kind="file", cls="delete", size=3, mtime_ns=1),
            CleanupEntry(path="notes.txt", kind="file", cls="left", size=4, mtime_ns=1),
        ],
        totals={"delete": 1, "left": 1},
        options=CleanupOptions(delete=["*.nfo"]),
        created_at=T0 + timedelta(minutes=1),
    )


def seed(db: AppDB) -> str:
    job = waiting_run(db, holds=[])
    AppCleanupDecisions(db).add(decision("c1", job.run))
    return job.run


async def test_FILES_CLEANUP_14_queue_lists_pending_cleanups(api: Api) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "cleanup"]
    assert (item["id"], item["run_id"], item["step_id"]) == ("c1", run_id, "clean")
    assert item["cleanup_decision"]["entries"][0]["path"] == "info.nfo"
    store = AppCleanupDecisions(api.state.db)
    done = store.get("c1")
    assert done is not None
    done.state = "kept"
    store.release(done)
    items = (await api.client.get("/api/review")).json()
    assert [i for i in items if i["kind"] == "cleanup"] == []


async def test_FILES_CLEANUP_14_get_returns_the_decision(api: Api) -> None:
    seed(api.state.db)
    r = await api.client.get("/api/cleanups/c1")
    assert r.status_code == 200
    assert r.json()["totals"] == {"delete": 1, "left": 1}
    assert (await api.client.get("/api/cleanups/nope")).status_code == 404


async def test_FILES_CLEANUP_14_delete_queues_a_job_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/cleanups/c1/decision", {"decision": "delete", "note": "fine"})
    assert r.status_code == 202
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["by"], job.payload["note"]) == ("cleanup_apply", "ui", "fine")
    again = await post(api, "/api/cleanups/c1/decision", {"decision": "keep"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")


async def test_FILES_CLEANUP_14_keep_resolves_at_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/cleanups/c1/decision", {"decision": "keep"})
    assert (r.status_code, r.json()) == (202, {"job_id": None})
    done = AppCleanupDecisions(api.state.db).get("c1")
    assert done is not None
    assert (done.state, done.decided_by) == ("kept", "ui")
    again = await post(api, "/api/cleanups/c1/decision", {"decision": "delete"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_resolved")


async def test_FILES_CLEANUP_14_bad_answers(api: Api) -> None:
    seed(api.state.db)
    assert (await post(api, "/api/cleanups/nope/decision", {"decision": "keep"})).status_code == 404
    bad = await post(api, "/api/cleanups/c1/decision", {"decision": "wipe"})
    assert bad.status_code in (400, 422)
