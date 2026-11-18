"""S-NOTIFY through the server: rules fed by a real run (fake worker), worker problems,
the test route (NOTIFY-14) and the review queue run filter (NOTIFY-16)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rifferr.api.app import create_api
from rifferr.api.security import secure
from rifferr.core.notify import Notification, SendResult
from rifferr.core.profile import Secret
from rifferr.profile_store import PROFILE_FILE, config_root, create_profile, profile_dir
from tests.api.conftest import Api, auth_client, settings, wf

PROFILE = """\
secrets:
  ntfy: env:RIFFERR_TEST_NTFY
notify:
  base_url: https://r.lan
  retry: []
  targets:
    phone: ntfy
  rules:
    - on: [run.failed, run.summary, worker.problem]
"""


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Notification]] = []
        self.fail = False

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        self.sent.append((target, urls.reveal(), n))
        if self.fail:
            return SendResult(target=target, ok=False, error=f"refused by {urls.reveal()}")
        return SendResult(target=target, ok=True)


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


async def _api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier, **kw: Any
) -> AsyncIterator[Api]:
    monkeypatch.setenv("RIFFERR_TEST_HEARTBEAT", "0.2")
    monkeypatch.setenv("RIFFERR_TEST_NTFY", "ntfys://user:s3cret@ntfy.example/music")
    create_profile(config_root(), "main")
    (profile_dir(config_root(), "main") / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    s = settings(tmp_path, notifier=notifier, **kw)
    app = create_api(s)
    async with (
        app.router.lifespan_context(app),
        auth_client(secure(app, s.security()), s.token) as client,
    ):
        yield Api(app, s, client)


@pytest.fixture
async def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier
) -> AsyncIterator[Api]:
    async for a in _api(tmp_path, monkeypatch, notifier):
        yield a


async def _until(cond: Any, timeout: float = 10) -> None:
    async def poll() -> None:
        while not cond():
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout)


async def test_NOTIFY_9_failed_run_notifies_from_the_server(
    api: Api, notifier: FakeNotifier
) -> None:
    api.add_workflow("boom", wf("{id: crash, uses: test.crash}"))
    run_id = await api.start("boom")
    await api.wait_finished(run_id)
    await _until(lambda: len(notifier.sent) >= 1)
    await api.state.notifications.drain()  # type: ignore[union-attr]
    titles = [n.title for _, _, n in notifier.sent]
    assert titles == ["Run failed: boom"]  # no albums: no summary (NOTIFY-8)
    failed = notifier.sent[0][2]
    assert "at step 'crash'" in failed.body
    assert failed.url == f"https://r.lan/runs/{run_id}"
    assert notifier.sent[0][1] == "ntfys://user:s3cret@ntfy.example/music"


async def test_NOTIFY_4_rule_outcome_events_reach_the_dashboard_stream(
    api: Api, notifier: FakeNotifier
) -> None:
    from rifferr.api.state import on_all_stream

    notifier.fail = True
    api.add_workflow("boom", wf("{id: crash, uses: test.crash}"))
    queue = api.state.hub.subscribe(None)
    run_id = await api.start("boom")
    await api.wait_finished(run_id)

    async def failed_event() -> Any:
        while True:
            e = await queue.get()
            if e.type == "notify.failed":
                return e

    event = await asyncio.wait_for(failed_event(), 10)
    assert on_all_stream(event)
    assert "s3cret" not in str(event.data)
    assert event.data["run_id"] == run_id


async def test_NOTIFY_8_worker_problem_reaches_the_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, notifier: FakeNotifier
) -> None:
    broken = [sys.executable, "-c", "import sys; sys.exit(3)"]
    async for api in _api(tmp_path, monkeypatch, notifier, worker_command=lambda p: broken):
        api.add_workflow("echo", wf("{id: a, uses: test.echo, with: {message: hi}}"))
        run_id = await api.start("echo")
        await api.wait_finished(run_id)
        await _until(lambda: any("worker" in n.title for _, _, n in notifier.sent))
        problem = next(n for _, _, n in notifier.sent if "worker" in n.title)
        assert "could not start" in problem.body
        assert problem.url == "https://r.lan/"


async def test_NOTIFY_14_test_route_sends_once(api: Api, notifier: FakeNotifier) -> None:
    r = await api.client.post("/api/profiles/main/notify/test", json={"target": "phone"})
    assert r.status_code == 200, r.text
    assert r.json() == {"target": "phone", "sent": True, "error": None}
    ((target, _, n),) = notifier.sent
    assert target == "phone"
    assert n.title == "Rifferr test notification"


async def test_NOTIFY_14_test_route_failure_is_redacted(api: Api, notifier: FakeNotifier) -> None:
    notifier.fail = True
    r = await api.client.post("/api/profiles/main/notify/test", json={"target": "phone"})
    body = r.json()
    assert body["sent"] is False
    assert "s3cret" not in body["error"]
    assert len(notifier.sent) == 1  # no retries for a test


async def test_NOTIFY_14_unknown_target_and_profile(api: Api) -> None:
    r = await api.client.post("/api/profiles/main/notify/test", json={"target": "pager"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "unknown_target"
    r = await api.client.post("/api/profiles/nope/notify/test", json={"target": "phone"})
    assert r.status_code == 404


async def test_NOTIFY_14_test_route_is_json_only(api: Api) -> None:
    r = await api.client.post(
        "/api/profiles/main/notify/test",
        content="target=phone",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415


async def test_NOTIFY_16_review_queue_filters_by_run(api: Api) -> None:
    r = await api.client.get("/api/review", params={"profile": "main", "run": "01UNKNOWN"})
    assert r.status_code == 200
    assert r.json() == []
