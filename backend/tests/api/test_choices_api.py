"""S-TAGS-CHOOSE choice reviews in the review queue and their decisions (TAGS-CHOOSE-3, -5,
-6)."""

from __future__ import annotations

from rifferr.appdb import AppChoiceReviews, AppDB
from rifferr.core.changeset import TargetRef
from rifferr.core.choices import ChoiceOffer, ChoicePreview, ChoiceReview, CustomField
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, post, waiting_run


def seed(db: AppDB) -> str:
    job = waiting_run(db, holds=[])
    AppChoiceReviews(db).add(
        ChoiceReview(
            id="c1",
            profile="main",
            run_id=job.run,
            step_id="kind",
            album_id="album-1",
            label="A & B - Split",
            options=[
                ChoiceOffer(
                    key="o0",
                    label="Split",
                    actions=[{"set": {"albumartist": "Split"}}],
                    preview=[
                        ChoicePreview(
                            target=TargetRef(scope="album", id="album-1"),
                            field="albumartist",
                            before="A & B",
                            after="Split",
                        )
                    ],
                )
            ],
            suggested="o0",
            custom=[CustomField(field="albumartist", value="A & B")],
            created_at=T0,
        )
    )
    return job.run


async def test_TAGS_CHOOSE_3_queue_lists_choice_reviews(api: Api) -> None:
    run_id = seed(api.state.db)
    items = (await api.client.get("/api/review")).json()
    (item,) = [i for i in items if i["kind"] == "choice"]
    assert (item["id"], item["run_id"], item["step_id"]) == ("c1", run_id, "kind")
    assert item["choice_review"]["suggested"] == "o0"
    assert (await api.client.get("/api/choices/c1")).json()["album_id"] == "album-1"
    assert (await api.client.get("/api/choices/nope")).status_code == 404


async def test_TAGS_CHOOSE_5_choose_queues_the_job_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/choices/c1/decision", {"decision": "choose", "option": "o0"})
    assert r.status_code == 202, r.text
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["review_id"], job.payload["option"], job.payload["by"]) == (
        "choice_apply",
        "c1",
        "o0",
        "ui",
    )
    again = await post(api, "/api/choices/c1/decision", {"decision": "skip"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "busy")


async def test_TAGS_CHOOSE_5_a_decision_that_does_not_fit_is_a_bad_request(api: Api) -> None:
    seed(api.state.db)
    r = await post(
        api,
        "/api/choices/c1/decision",
        {"decision": "choose", "option": "custom", "values": {"title": "x"}},
    )
    assert (r.status_code, r.json()["error"]["code"]) == (400, "bad_request")


async def test_TAGS_CHOOSE_6_skip_resolves_at_once(api: Api) -> None:
    seed(api.state.db)
    r = await post(api, "/api/choices/c1/decision", {"decision": "skip", "note": "later"})
    assert (r.status_code, r.json()) == (202, {"job_id": None})
    done = AppChoiceReviews(api.state.db).get("c1")
    assert done is not None
    assert (done.state, done.decided_by, done.note) == ("skipped", "ui", "later")
    again = await post(api, "/api/choices/c1/decision", {"decision": "reject"})
    assert (again.status_code, again.json()["error"]["code"]) == (409, "already_resolved")
