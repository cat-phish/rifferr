"""Shared AI routes: preview, job status and cancel (AI-CORE-18, -19)."""

from __future__ import annotations

import asyncio
from typing import Any

from rifferr.ai.testing import FakeAIProvider
from rifferr.engine.events import profile_stream
from rifferr.profile_store import config_root, profile_dir
from tests.ai.test_runner import ANSWER, PickIn, PickTask
from tests.api.conftest import Api

AI_SECTION = """
ai:
  providers:
    - name: fake
      type: fake
      model: m1
"""


def configure(api: Api, section: str = AI_SECTION) -> FakeAIProvider:
    path = profile_dir(config_root(), "main") / "profile.yaml"
    path.write_text(path.read_text(encoding="utf-8") + section, encoding="utf-8")
    fake = FakeAIProvider()
    service = api.state.ai
    assert service is not None
    service.providers = {"fake": fake}
    service.tasks = {"test.pick": PickTask()}
    return fake


async def test_AI_CORE_18_preview_route(api: Api) -> None:
    fake = configure(api)
    r = await api.client.post(
        "/api/ai/preview",
        json={"profile": "main", "task": "test.pick", "input": {"album": "Doolittle"}},
    )
    assert r.status_code == 200, r.text
    body: dict[str, Any] = r.json()
    assert body["user"].startswith("Album: Doolittle")
    assert body["provider"] == "fake"
    assert body["host"] == "fake.invalid"
    assert body["cached"] is False
    assert body["sends"] == ["album"]
    assert fake.requests == []


async def test_AI_CORE_18_preview_errors(api: Api) -> None:
    configure(api)
    post = api.client.post
    r = await post("/api/ai/preview", json={"profile": "main", "task": "nope", "input": {}})
    assert r.status_code == 404
    r = await post("/api/ai/preview", json={"profile": "main", "task": "test.pick", "input": {}})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_input"
    r = await post(
        "/api/ai/preview",
        json={"profile": "main", "task": "test.pick", "input": {"album": "A"}, "provider": "x"},
    )
    assert r.status_code == 400
    assert "no AI provider named 'x'" in r.json()["error"]["message"]
    r = await post("/api/ai/preview", json={"profile": "ghost", "task": "test.pick"})
    assert r.status_code in (404, 422)


async def test_AI_CORE_5_preview_without_ai_section(api: Api) -> None:
    configure(api, section="")
    r = await api.client.post(
        "/api/ai/preview", json={"profile": "main", "task": "test.pick", "input": {"album": "A"}}
    )
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "AI is not configured for this profile"


async def test_AI_CORE_19_job_status_cancel_and_events(api: Api) -> None:
    fake = configure(api)
    fake.script(ANSWER, ANSWER)
    service = api.state.ai
    assert service is not None
    rt = service.runtime("main")
    task = PickTask()
    work = [(a, lambda a=a: rt.run_task(task, PickIn(album=a), target=a)) for a in ("A", "B")]
    job = service.jobs.start("main", task.id, work, concurrency=1)
    assert job.runner is not None
    await asyncio.wait_for(job.runner, 5)
    r = await api.client.get(f"/api/ai/jobs/{job.id}")
    assert r.status_code == 200
    assert r.json()["state"] == "succeeded"
    assert r.json()["done"] == 2
    r = await api.client.post(f"/api/ai/jobs/{job.id}/cancel", json={})
    assert r.status_code == 202  # already finished: nothing to stop
    assert (await api.client.get("/api/ai/jobs/nope")).status_code == 404
    assert (await api.client.post("/api/ai/jobs/nope/cancel", json={})).status_code == 404
    types = [e.type for e in api.state.db.events_since(profile_stream("main"), 0)]
    assert "ai.job.finished" in types
    assert "ai.request" in types  # run_task events of a server-side job go to the profile
