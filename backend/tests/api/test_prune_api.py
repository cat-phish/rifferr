"""S-LIBRARY-PRUNE routes: prune decisions in the queue and their decision
(LIBRARY-PRUNE-3)."""

from __future__ import annotations

from datetime import timedelta

from rifferr.appdb import AppDB, AppPruneDecisions
from rifferr.core.prune import PruneAlbum, PruneDecision
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run


def seed(db: AppDB) -> str:
    job = waiting_run(db, holds=[])
    AppPruneDecisions(db).add(
        PruneDecision(
            id="p1",
            profile="main",
            run_id=job.run,
            step_id="prune",
            albums=[
                PruneAlbum(album_id="7", label="Band - Gone", path="/m/Gone", items=2, missing=2)
            ],
            created_at=T0 + timedelta(minutes=1),
        )
    )
    return job.run


async def test_LIBRARY_PRUNE_3_queue_lists_pending_prunes(api: Api) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "prune"]
    assert (item["id"], item["run_id"], item["step_id"]) == ("p1", run_id, "prune")
    assert item["prune_decision"]["albums"][0]["label"] == "Band - Gone"


async def test_LIBRARY_PRUNE_3_accept_queues_a_job_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/prunes/p1/decision", {"decision": "accept", "note": "fine"})
    assert r.status_code == 202
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["by"], job.payload["note"]) == ("prune_apply", "ui", "fine")
    again = await post(api, "/api/prunes/p1/decision", {"decision": "reject"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")


async def test_LIBRARY_PRUNE_3_reject_resolves_at_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/prunes/p1/decision", {"decision": "reject"})
    assert (r.status_code, r.json()) == (202, {"job_id": None})
    done = AppPruneDecisions(api.state.db).get("p1")
    assert done is not None
    assert (done.state, done.decided_by) == ("rejected", "ui")
    items = (await api.client.get("/api/review")).json()
    assert [i for i in items if i["kind"] == "prune"] == []
    again = await post(api, "/api/prunes/p1/decision", {"decision": "accept"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_resolved")
    assert (await post(api, "/api/prunes/nope/decision", {"decision": "accept"})).status_code == 404
