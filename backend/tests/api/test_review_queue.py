"""F-REVIEW-QUEUE routes: the queue, ChangeSet detail and current values, decisions,
re-propose, gate decisions, per-run decisions, run undo and /api/events."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from rifferr.appdb import AppChangeSets, AppDB, AppJournal, AppReviewGates, Job
from rifferr.core.changeset import (
    ApplyResult,
    ChangeSet,
    ChangeSource,
    FieldChange,
    FieldEffect,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from rifferr.core.review import GateAlbum, ReviewGate
from rifferr.engine import Event
from tests.api.conftest import ECHO, Api
from tests.api.test_library import stream

T0 = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
SOURCE = ChangeSource(kind="step", name="tags.script")


def waiting_run(db: AppDB, holds: list[str] = (), gates: list[str] = ()) -> Job:  # type: ignore[assignment]
    """A run suspended for review, as the worker leaves it (RUN-14)."""
    job = db.submit("main", {"source": ECHO, "vars": {}}, workflow="tagging")
    assert db.claim_next("main") is not None
    assert db.suspend(job.run, "p", list(holds), list(gates))
    return job


def changeset(cs_id: str, run_id: str | None, *, minutes: int = 0, target: str = "a1") -> ChangeSet:
    return ChangeSet(
        id=cs_id,
        profile="main",
        run_id=run_id,
        step_id="p" if run_id else None,
        source=SOURCE,
        created_at=T0 + timedelta(minutes=minutes),
        targets=[
            TargetChange(
                target=TargetRef(scope="album", id=target),
                changes=[
                    FieldChange(field="album", old="Live", new="Live (EP)"),
                    FieldChange(field="albumdisambig", old=None, new="EP"),
                ],
            )
        ],
    )


def gate(gate_id: str, run_id: str, *, minutes: int = 0, on_reject: str = "stop") -> ReviewGate:
    return ReviewGate(
        id=gate_id,
        profile="main",
        run_id=run_id,
        step_id="check",
        albums=[GateAlbum(target=TargetRef(scope="album", id="a1"), label="Band - Live")],
        on_reject=on_reject,  # type: ignore[arg-type]
        on_timeout="continue",
        created_at=T0 + timedelta(minutes=minutes),
        deadline=T0 + timedelta(days=30),
    )


def seed(api: Api) -> tuple[str, str]:
    """Two waiting runs: one with a proposed and a conflicted ChangeSet, one with a gate."""
    db = api.state.db
    store = AppChangeSets(db)
    first = waiting_run(db, holds=["cs1", "cs2"])
    store.add(changeset("cs1", first.run, minutes=1))
    store.add(changeset("cs2", first.run, minutes=3))
    store.set_state("cs2", "conflicted", resolved_by="ui")
    store.add(changeset("manual", None, minutes=0))  # tag grid: never in the queue
    store.add(changeset("done", first.run, minutes=0))
    store.set_state("done", "applied")
    second = waiting_run(db, gates=["g1"])
    AppReviewGates(db).add(gate("g1", second.run, minutes=2))
    return first.run, second.run


async def post(api: Api, path: str, body: dict[str, Any] | None = None) -> Any:
    return await api.client.post(path, json=body if body is not None else {})


async def wait_job(api: Api, job_id: str, timeout: float = 15) -> Job:
    async def poll() -> Job:
        while True:
            job = api.state.db.job(job_id)
            if job is not None and job.state in ("succeeded", "failed", "cancelled"):
                return job
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(poll(), timeout)


# -- REVIEW-QUEUE-1 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_1_lists_waiting_changesets_and_gates_oldest_first(api: Api) -> None:
    first, second = seed(api)
    r = await api.client.get("/api/review")
    assert r.status_code == 200
    items = r.json()
    assert [(i["kind"], i["id"]) for i in items] == [
        ("changeset", "cs1"),
        ("gate", "g1"),
        ("changeset", "cs2"),
    ]
    cs1, g1, cs2 = items
    assert (cs1["run_id"], cs1["workflow"], cs1["step_id"], cs1["profile"]) == (
        first,
        "tagging",
        "p",
        "main",
    )
    assert cs1["state"] == "proposed"
    assert cs1["changeset"]["id"] == "cs1"
    assert cs2["state"] == "conflicted"
    assert (g1["run_id"], g1["step_id"], g1["gate"]["on_reject"]) == (second, "check", "stop")
    only = (await api.client.get("/api/review", params={"run": second})).json()
    assert [i["id"] for i in only] == ["g1"]
    assert (await api.client.get("/api/review", params={"profile": "other"})).json() == []


# -- REVIEW-QUEUE-2 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_2_changeset_detail(api: Api) -> None:
    seed(api)
    db = api.state.db
    AppChangeSets(db).set_state("cs2", "conflicted", decisions={0: True, 1: False})
    AppJournal(db).begin(
        JournalEntry(
            id="j1",
            profile="main",
            changeset_id="cs2",
            intended=changeset("cs2", None).targets,
            created_at=T0,
        )
    )
    r = await api.client.get("/api/changesets/cs2")
    assert r.status_code == 200
    body = r.json()
    assert (body["state"], body["resolved_by"]) == ("conflicted", "ui")
    assert body["decisions"] == {"0": True, "1": False}
    assert body["journal_ids"] == ["j1"]
    assert body["changeset"]["id"] == "cs2"
    r = await api.client.get("/api/changesets/nope")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "not_found")


# -- REVIEW-QUEUE-3 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_3_current_values_come_from_the_read_only_worker(api: Api) -> None:
    page = (await api.client.get("/api/profiles/main/library")).json()
    album = page["albums"][1]
    item = album["items"][0]
    cs = ChangeSet(
        id="cur",
        profile="main",
        run_id="r",
        source=SOURCE,
        created_at=T0,
        targets=[
            TargetChange(
                target=TargetRef(scope="album", id=album["id"]),
                changes=[FieldChange(field="album", old="x", new="y")],
            ),
            TargetChange(
                target=TargetRef(scope="item", id=item["id"]),
                changes=[FieldChange(field="title", old="x", new="y")],
            ),
            TargetChange(
                target=TargetRef(scope="album", id="gone"),
                changes=[FieldChange(field="album", old="x", new="y")],
            ),
        ],
    )
    AppChangeSets(api.state.db).add(cs)
    r = await api.client.get("/api/changesets/cur/current")
    assert r.status_code == 200, r.text
    targets = r.json()["targets"]
    assert [(t["label"], t["missing"], t["fields"]) for t in targets] == [
        ("Album 1", False, {"album": "Album 1"}),
        ("1. a/b", False, {"title": "a/b"}),
        ("album gone", True, {}),
    ]
    assert (await api.client.get("/api/changesets/nope/current")).status_code == 404


# -- REVIEW-QUEUE-4 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_4_decision_queues_a_job(api: Api) -> None:
    seed(api)
    r = await post(api, "/api/changesets/cs1/decision", {"decision": "accept", "accepted": [0]})
    assert r.status_code == 202, r.text
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert job.kind == "review_decision"
    assert job.payload["accepted"] == [0]
    assert job.payload["by"] == "ui"


async def test_REVIEW_QUEUE_4_decision_errors(api: Api) -> None:
    seed(api)

    async def code(cs_id: str, body: dict[str, Any]) -> tuple[int, str]:
        r = await post(api, f"/api/changesets/{cs_id}/decision", body)
        return r.status_code, r.json()["error"]["code"]

    assert await code("nope", {"decision": "accept"}) == (404, "not_found")
    assert await code("manual", {"decision": "reject"}) == (404, "not_found")
    assert await code("done", {"decision": "reject"}) == (409, "already_resolved")
    assert await code("cs2", {"decision": "accept"}) == (409, "conflicted")
    assert await code("cs1", {"decision": "accept", "accepted": [7]}) == (400, "bad_request")
    assert await code("cs1", {"decision": "accept", "accepted": []}) == (400, "bad_request")
    assert await code("cs1", {"decision": "maybe"}) == (400, "bad_request")
    assert api.state.db.jobs("main", "queued") == []


async def test_REVIEW_QUEUE_4_reject_resolves_it_in_the_worker(api: Api) -> None:
    seed(api)
    r = await post(api, "/api/changesets/cs2/decision", {"decision": "reject", "note": "no"})
    assert r.status_code == 202
    job = await wait_job(api, r.json()["job_id"])
    assert job.state == "succeeded"
    detail = (await api.client.get("/api/changesets/cs2")).json()
    assert (detail["state"], detail["resolved_by"], detail["note"]) == ("rejected", "ui", "no")


# -- REVIEW-QUEUE-5 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_5_repropose_route(api: Api) -> None:
    seed(api)
    r = await post(api, "/api/changesets/cs1/repropose")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_conflicted")
    r = await post(api, "/api/changesets/done/repropose")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "already_resolved")
    assert (await post(api, "/api/changesets/nope/repropose")).status_code == 404
    r = await post(api, "/api/changesets/cs2/repropose")
    assert r.status_code == 202
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["changeset_id"]) == ("repropose", "cs2")


# -- REVIEW-QUEUE-6 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_6_gate_decision(api: Api) -> None:
    _, second = seed(api)
    r = await post(api, "/api/reviews/g1/decision", {"decision": "reject", "note": "bad tags"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["state"], body["decided_by"], body["note"]) == ("rejected", "ui", "bad tags")
    run = (await api.client.get(f"/api/runs/{second}")).json()
    assert run["state"] == "cancelled"
    r = await post(api, "/api/reviews/g1/decision", {"decision": "continue"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "already_resolved")
    r = await post(api, "/api/reviews/nope/decision", {"decision": "continue"})
    assert r.status_code == 404


async def test_REVIEW_QUEUE_6_gate_of_a_run_not_yet_waiting(api: Api) -> None:
    db = api.state.db
    job = db.submit("main", {"source": ECHO, "vars": {}})
    assert db.claim_next("main") is not None  # running, not suspended yet
    AppReviewGates(db).add(gate("g2", job.run))
    r = await post(api, "/api/reviews/g2/decision", {"decision": "continue"})
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_waiting")


# -- REVIEW-QUEUE-7 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_7_decide_every_proposed_changeset_of_a_run(api: Api) -> None:
    first, _ = seed(api)
    store = AppChangeSets(api.state.db)
    store.add(changeset("cs3", first, minutes=4, target="a2"))
    r = await post(api, f"/api/runs/{first}/decisions", {"decision": "reject"})
    assert r.status_code == 202, r.text
    job_ids = r.json()["job_ids"]
    assert len(job_ids) == 2  # cs1 and cs3; the conflicted cs2 is left alone
    decided = {api.state.db.job(j).payload["changeset_id"] for j in job_ids}  # type: ignore[union-attr]
    assert decided == {"cs1", "cs3"}
    for j in job_ids:
        assert (await wait_job(api, j)).state == "succeeded"
    stored = store.get("cs2")
    assert stored is not None
    assert stored.state == "conflicted"
    r = await post(api, "/api/runs/nope/decisions", {"decision": "accept"})
    assert r.status_code == 404


# -- REVIEW-QUEUE-8 -----------------------------------------------------------------------


async def test_REVIEW_QUEUE_8_run_undo(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    run_id = await api.start("echo")
    await api.wait_finished(run_id)
    r = await post(api, f"/api/runs/{run_id}/undo")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_undoable")
    journal = AppJournal(api.state.db)
    target = TargetRef(scope="album", id="a1")
    journal.begin(
        JournalEntry(
            id="j1",
            profile="main",
            changeset_id="cs",
            run_id=run_id,
            intended=[
                TargetChange(target=target, changes=[FieldChange(field="year", old=1, new=2)])
            ],
            created_at=T0,
        )
    )
    effect = FieldEffect(target=target, field="year", before=1, after=2, existed_before=True)
    assert (await api.client.get(f"/api/runs/{run_id}")).json()["undoable"] == 0
    journal.complete("j1", ApplyResult(applied=True, effects=[effect]))
    assert (await api.client.get(f"/api/runs/{run_id}")).json()["undoable"] == 1
    r = await post(api, f"/api/runs/{run_id}/undo")
    assert r.status_code == 202, r.text
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["run_id"], job.payload["by"]) == ("undo", run_id, "ui")
    assert (await post(api, "/api/runs/nope/undo")).status_code == 404

    # Still queued or running: nothing to undo yet.
    db = api.state.db
    running = db.submit("main", {"source": ECHO, "vars": {}})
    r = await post(api, f"/api/runs/{running.run}/undo")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_undoable")


# -- REVIEW-QUEUE-10 ----------------------------------------------------------------------


async def test_REVIEW_QUEUE_10_all_events_carry_review_and_changeset_events(api: Api) -> None:
    _, second = seed(api)
    async with stream(api, "/api/events") as until:
        await asyncio.sleep(0.1)  # subscribed
        await post(api, "/api/reviews/g1/decision", {"decision": "reject"})
        seen = await until("run.finished")
    types = [m["event"] for m in seen]
    assert "review.resolved" in types
    assert types[-1] == "run.finished"
    db = api.state.db

    def emit(run: str, type: str) -> None:
        seq = db.last_seq(run) + 1
        event = Event(seq=seq, ts=datetime.now(UTC), run_id=run, type=type, data={})
        api.state.supervisor._persist(event)  # pyright: ignore[reportPrivateUsage]

    async with stream(api, "/api/events") as until:
        await asyncio.sleep(0.1)
        emit("profile:main", "changeset.applied")  # the profile stream stays off it
        emit(second, "run.step.log")  # step-level stays off it
        for type in ("changeset.proposed", "run.waiting_review", "job.finished", "run.started"):
            emit(second, type)
        seen = await until("run.started")
    assert [m["event"] for m in seen] == [
        "changeset.proposed",
        "run.waiting_review",
        "job.finished",
        "run.started",
    ]
