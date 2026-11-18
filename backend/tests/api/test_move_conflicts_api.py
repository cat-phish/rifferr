"""S-LIBRARY-MOVE move conflicts in the review queue and their decisions (LIBRARY-MOVE-5a)."""

from __future__ import annotations

from rifferr.appdb import AppDB, AppMoveConflicts
from rifferr.core.moves import MoveConflict, MoveSettings
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run


def seed(db: AppDB) -> str:
    job = waiting_run(db, holds=[])
    AppMoveConflicts(db).add(
        MoveConflict(
            id="m1",
            profile="main",
            run_id=job.run,
            step_id="into",
            album_id="album-1",
            label="The Beatles - Abbey Road",
            conflicts=["/music/x/1.flac already exists"],
            src_dir="/inbox/Abbey Road",
            dst_dir="/music/x",
            settings=MoveSettings(directory="/music"),
            created_at=T0,
        )
    )
    return job.run


async def test_LIBRARY_MOVE_5a_queue_lists_move_conflicts(api: Api) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "move"]
    assert (item["id"], item["run_id"], item["step_id"]) == ("m1", run_id, "into")
    assert item["move_conflict"]["conflicts"] == ["/music/x/1.flac already exists"]
    assert (await api.client.get("/api/move-conflicts/m1")).json()["album_id"] == "album-1"
    assert (await api.client.get("/api/move-conflicts/nope")).status_code == 404


async def test_LIBRARY_MOVE_5a_retry_queues_the_job_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/move-conflicts/m1/decision", {"decision": "retry"})
    assert r.status_code == 202, r.text
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["conflict_id"], job.payload["by"]) == ("move_retry", "m1", "ui")
    again = await post(api, "/api/move-conflicts/m1/decision", {"decision": "fail"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")


async def test_LIBRARY_MOVE_5a_fail_resolves_at_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/move-conflicts/m1/decision", {"decision": "fail"})
    assert (r.status_code, r.json()) == (202, {"job_id": None})
    done = AppMoveConflicts(api.state.db).get("m1")
    assert done is not None
    assert (done.state, done.decided_by) == ("failed", "ui")
    again = await post(api, "/api/move-conflicts/m1/decision", {"decision": "retry"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_resolved")
    bad = await post(api, "/api/move-conflicts/nope/decision", {"decision": "retry"})
    assert bad.status_code == 404
