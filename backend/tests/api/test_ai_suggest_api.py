"""Ask AI and Suggest with AI for all on choice reviews (F-AI-SUGGEST AI-SUGGEST-9 to -11,
-14, -15)."""

from __future__ import annotations

import asyncio
from typing import Any

from rifferr.ai.base import AIError
from rifferr.ai.tasks.choice_suggest import ChoiceSuggestTask
from rifferr.ai.testing import FakeAIProvider
from rifferr.appdb import AppChoiceReviews, AppDB
from rifferr.core.choices import (
    ChoiceOffer,
    ChoiceReview,
    ChoiceSuggestInput,
    CustomField,
    SuggestOption,
    SuggestRule,
)
from rifferr.engine.events import profile_stream
from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import Api
from tests.api.test_review_queue import T0, waiting_run

AI_SECTION = """
ai:
  providers:
    - name: fake
      type: fake
      model: m1
      settings: {price_per_mtok: {input: 1, output: 5}}
"""


def configure(api: Api) -> FakeAIProvider:
    path = profile_dir(config_root(), "main") / "profile.yaml"
    path.write_text(path.read_text(encoding="utf-8") + AI_SECTION, encoding="utf-8")
    fake = FakeAIProvider()
    service = api.state.ai
    assert service is not None
    service.providers = {"fake": fake}
    service.tasks = {"choice.suggest": ChoiceSuggestTask()}
    return fake


def ai_input(album: str = "GBH - City Baby Attacked by Rats") -> ChoiceSuggestInput:
    return ChoiceSuggestInput(
        album=album,
        options=[SuggestOption(key="f0", label="Oi", fallback=True, rule=True, matched=1)],
        rules=[
            SuggestRule(label="Punk", actions=[{"set": {"genres": ["Punk"]}}]),
            SuggestRule(label="Alt／Indie", actions=[{"set": {"genres": ["Alt／Indie"]}}]),  # noqa: RUF001
        ],
        custom=["genres"],
        context={"albumartist": "GBH"},
    )


def seed(db: AppDB, review_id: str = "c1", *, ai: bool = True) -> ChoiceReview:
    job = waiting_run(db, holds=[])
    review = ChoiceReview(
        id=review_id,
        profile="main",
        run_id=job.run,
        step_id="bucket",
        album_id=f"album-{review_id}",
        label="GBH - City Baby Attacked by Rats",
        options=[ChoiceOffer(key="f0", label="Oi", actions=[{"set": {"genres": ["Oi"]}}])],
        custom=[CustomField(field="genres", kind="list", value=[])],
        created_at=T0,
        ai_input=ai_input(f"GBH - Album {review_id}") if ai else None,
    )
    AppChoiceReviews(db).add(review)
    return review


def answer(*choices: str) -> dict[str, Any]:
    return {"suggestions": [{"choice": c, "confidence": 0.7, "rationale": "r"} for c in choices]}


async def test_AI_SUGGEST_10_ask_ai_stores_suggestions(api: Api) -> None:
    fake = configure(api)
    seed(api.state.db)
    fake.script(answer("punk", "Oi", "Street Punk"))
    r = await api.client.post("/api/choices/c1/ai-suggest", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    got = [(s["label"], s["option"], s["new"]) for s in body["ai"]["suggestions"]]
    assert got == [("Punk", "a0", False), ("Oi", "f0", False), ("Street Punk", None, True)]
    assert [o["key"] for o in body["options"]] == ["f0", "a0"]
    assert body["state"] == "pending"  # nothing decided (AI-SUGGEST-15)
    assert body["suggested"] is None
    stored = AppChoiceReviews(api.state.db).get("c1")
    assert stored is not None
    assert stored.ai is not None
    assert stored.ai.provider == "fake"
    types = [e.type for e in api.state.db.events_since(profile_stream("main"), 0)]
    assert "choice.ai_suggested" in types


async def test_AI_SUGGEST_9_asking_again_replaces_and_refresh_skips_cache(api: Api) -> None:
    fake = configure(api)
    seed(api.state.db)
    fake.script(answer("Punk"), answer("Alt/Indie"))
    await api.client.post("/api/choices/c1/ai-suggest", json={})
    r = await api.client.post("/api/choices/c1/ai-suggest", json={})  # cached: same answer
    assert [s["label"] for s in r.json()["ai"]["suggestions"]] == ["Punk"]
    assert r.json()["ai"]["cached"] is True
    r = await api.client.post("/api/choices/c1/ai-suggest", json={"refresh": True})
    assert [s["label"] for s in r.json()["ai"]["suggestions"]] == ["Alt／Indie"]  # noqa: RUF001
    assert [o["label"] for o in r.json()["options"]] == ["Oi", "Alt／Indie"]  # noqa: RUF001
    assert len(fake.requests) == 2


async def test_AI_SUGGEST_10_errors(api: Api) -> None:
    fake = configure(api)
    seed(api.state.db, "c1")
    seed(api.state.db, "c2", ai=False)
    assert (await api.client.post("/api/choices/nope/ai-suggest", json={})).status_code == 404
    r = await api.client.post("/api/choices/c2/ai-suggest", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "no_ai")
    fake.script(AIError("HTTP 401: bad key", retryable=False))
    r = await api.client.post("/api/choices/c1/ai-suggest", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (502, "ai_failed")
    stored = AppChoiceReviews(api.state.db).get("c1")
    assert stored is not None
    assert stored.ai is not None
    assert stored.ai.error == "HTTP 401: bad key"  # the card shows the note
    store = AppChoiceReviews(api.state.db)
    review = store.claim("c1", "job-1")
    review.state = "chosen"
    store.release(review)
    r = await api.client.post("/api/choices/c1/ai-suggest", json={})
    assert r.status_code == 409


async def test_AI_SUGGEST_14_estimate_counts_without_sending(api: Api) -> None:
    fake = configure(api)
    for i in (1, 2, 3):
        seed(api.state.db, f"c{i}")
    seed(api.state.db, "c4", ai=False)
    r = await api.client.post("/api/choices/ai-suggest/estimate", json={"profile": "main"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["input_tokens"] > 0
    assert body["cost_max"] is not None
    assert body["currency"] == "USD"
    assert fake.requests == []


async def test_AI_SUGGEST_11_bulk_job_skips_reviews_with_suggestions(api: Api) -> None:
    fake = configure(api)
    for i in (1, 2, 3):
        seed(api.state.db, f"c{i}")
    fake.script(answer("Punk"))
    await api.client.post("/api/choices/c1/ai-suggest", json={})
    fake.script(answer("Punk"), AIError("HTTP 500", retryable=False))
    r = await api.client.post("/api/choices/ai-suggest", json={"profile": "main"})
    assert r.status_code == 202, r.text
    started = r.json()
    assert started["total"] == 2
    service = api.state.ai
    assert service is not None
    job = service.jobs.get(started["job"])
    assert job is not None
    assert job.runner is not None
    await asyncio.wait_for(job.runner, 5)
    status = (await api.client.get(f"/api/ai/jobs/{job.id}")).json()
    assert (status["done"], status["failed"]) == (1, 1)
    store = AppChoiceReviews(api.state.db)
    assert all(r.state == "pending" for r in store.pending(profile="main"))  # AI-SUGGEST-15
    r = await api.client.post("/api/choices/ai-suggest", json={"ids": ["c1"]})
    assert r.json() == {"job": None, "total": 0}


async def test_AI_SUGGEST_6_decision_takes_the_suggestion_rank(api: Api) -> None:
    fake = configure(api)
    seed(api.state.db)
    fake.script(answer("Punk"))
    await api.client.post("/api/choices/c1/ai-suggest", json={})
    r = await api.client.post(
        "/api/choices/c1/decision",
        json={"decision": "choose", "option": "a0", "ai_suggestion": 1},
    )
    assert r.status_code == 202, r.text
    job = api.state.db.job(r.json()["job_id"])
    assert job is not None
    assert job.payload["ai_suggestion"] == 1
