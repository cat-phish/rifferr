"""WORKER-11 read-only worker and TAGSCRIPT-20/-21 preview."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rifferr.backends.fake import FakeBackend
from rifferr.core.profile import Profile
from rifferr.core.selection import Selection
from rifferr.profile_store import config_root, create_profile, load_profile, profile_dir
from rifferr.tagscript.preview import PreviewRequest, PreviewResponse
from rifferr.worker.previews import (
    PreviewFailed,
    PreviewSuperseded,
    PreviewSupervisor,
    PreviewTimeout,
    PreviewUnavailable,
)
from rifferr.worker.protocol import WorkerCommand, WorkerMessage
from rifferr.worker.readonly import ReadOnlyWorker, RequestFailed, run_preview
from tests.worker.conftest import FAKE_WORKER

EP = """\
actions:
  - name: ep-suffix
    when: 'albumtype:ep'
    set: { album: '$album (EP)' }
  - replace: { fields: [title], find: '/', with: '-' }
    scope: item
"""


def library(n: int = 2) -> FakeBackend:
    fb = FakeBackend()
    for i in range(n):
        fb.add_album(
            {
                "albumartist": "Band",
                "album": f"Album {i}",
                "albumtype": "ep" if i == 0 else "album",
                "items": [{"title": "a/b", "track": 1}],
            }
        )
    return fb


@pytest.fixture
def profile() -> Profile:
    create_profile(config_root(), "main")
    return load_profile(config_root(), "main")


def preview(profile: Profile, fb: FakeBackend, **request: Any) -> PreviewResponse:
    return run_preview(profile, fb.read_only_view(), PreviewRequest.model_validate(request))


# -- TAGSCRIPT-20: preview -------------------------------------------------------------


def test_TAGSCRIPT_20_preview_shows_before_after_actions_and_idempotence(
    profile: Profile,
) -> None:
    fb = library()
    r = preview(profile, fb, script_text=EP)
    assert r.validation_errors == []
    assert r.albums == 2
    assert not r.truncated
    album = next(t for t in r.targets if t.label == "Band - Album 0")
    (f,) = album.fields
    assert (f.field, f.before, f.after, f.actions) == (
        "album",
        "Album 0",
        "Album 0 (EP)",
        ["ep-suffix"],
    )
    assert album.actions == ["ep-suffix"]
    items = [t for t in r.targets if t.target.scope == "item"]
    assert [t.fields[0].after for t in items] == ["a-b", "a-b"]
    assert items[0].label == "1. a/b"
    (issue,) = r.idempotence
    assert issue.action == "ep-suffix"
    assert issue.second == "Album 0 (EP) (EP)"
    assert r.idempotence_checked


def test_TAGSCRIPT_20_preview_writes_nothing(profile: Profile) -> None:
    fb = library()
    preview(profile, fb, script_text=EP)
    assert fb.apply_calls == []
    assert sorted(str(a.fields["album"]) for a in fb.query(Selection())) == ["Album 0", "Album 1"]


def test_TAGSCRIPT_20_query_and_limit(profile: Profile) -> None:
    fb = library(5)
    r = preview(profile, fb, script_text=EP, query="albumtype:album", limit=2)
    assert r.albums == 2
    assert r.truncated
    r = preview(profile, fb, script_text=EP, query="albumtype:ep")
    assert r.albums == 1
    assert not r.truncated


def test_TAGSCRIPT_20_limit_bounds() -> None:
    assert PreviewRequest().limit == 50
    with pytest.raises(ValueError, match="less than or equal to 500"):
        PreviewRequest(limit=501)


def test_TAGSCRIPT_20_validation_errors_have_line_and_column(profile: Profile) -> None:
    r = preview(profile, library(), script_text="actions:\n  - sett: {a: 1}\n")
    (e,) = r.validation_errors
    assert (e.line, e.column, e.path) == (2, 5, "actions[0]")
    assert r.targets == []
    r = preview(profile, library(), script_text="actions: [\n")
    assert r.validation_errors[0].line is not None
    r = preview(profile, library(), script_text=EP, query="bare-term")
    assert r.validation_errors[0].path == "query"


def test_TAGSCRIPT_20_script_file_and_errors(profile: Profile) -> None:
    scripts = profile_dir(config_root(), "main") / "scripts"
    scripts.mkdir()
    (scripts / "ep.yaml").write_text(EP)
    r = preview(profile, library(), script="ep.yaml")
    assert r.albums == 2
    with pytest.raises(RequestFailed) as exc:
        preview(profile, library(), script="missing.yaml")
    assert exc.value.code == "script_not_found"
    with pytest.raises(RequestFailed):
        preview(profile, library(), script="x.yaml", script_text=EP)


def test_TAGSCRIPT_20_errors_per_target(profile: Profile) -> None:
    r = preview(profile, library(), script_text="actions:\n  - set: {year: 'soon'}\n")
    errors = [e for t in r.targets for e in t.errors]
    assert len(errors) == 2
    assert all("expects a whole number" in e for e in errors)
    assert all(t.fields == [] for t in r.targets)


def test_TAGSCRIPT_20_scope_override_is_checked(profile: Profile) -> None:
    text = "actions:\n  - set: {album: x}\n    scope: album\n"
    r = preview(profile, library(), script_text=text, scope="item")
    assert "album-scoped action" in r.validation_errors[0].message


def test_TAGSCRIPT_21_cancelled_preview_stops_between_albums(profile: Profile) -> None:
    fb = library(4)
    calls = iter(range(100))
    r = run_preview(
        profile,
        fb.read_only_view(),
        PreviewRequest(script_text=EP),
        cancelled=lambda: next(calls) >= 1,
    )
    assert r.albums == 1
    assert r.truncated


# -- WORKER-11: the read-only worker, in process ------------------------------------------


def test_WORKER_11_refuses_a_writable_backend(profile: Profile) -> None:
    with pytest.raises(ValueError, match="read-only"):
        ReadOnlyWorker(profile, library(), lambda m: None)


async def test_WORKER_11_answers_requests_and_refuses_run_jobs(profile: Profile) -> None:
    sent: list[WorkerMessage] = []
    worker = ReadOnlyWorker(profile, library().read_only_view(), sent.append)
    queue: asyncio.Queue[WorkerCommand | None] = asyncio.Queue()
    task = asyncio.create_task(worker.serve(queue))
    queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    queue.put_nowait(
        WorkerCommand(
            kind="request",
            profile="main",
            request_id="r1",
            request={"kind": "tagscript.preview", "payload": {"script_text": EP}},
        )
    )
    queue.put_nowait(
        WorkerCommand(kind="request", profile="main", request_id="r2", request={"kind": "x"})
    )

    async def answered() -> None:
        while len([m for m in sent if m.kind == "response"]) < 2:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(answered(), 5)
    queue.put_nowait(None)
    await asyncio.wait_for(task, 5)
    kinds = [m.kind for m in sent]
    assert kinds[0] == "ready"
    assert any(m.kind == "error" and "no run" in (m.message or "") for m in sent)
    r1 = next(m for m in sent if m.request_id == "r1")
    assert r1.result is not None
    assert PreviewResponse.model_validate(r1.result).albums == 2
    r2 = next(m for m in sent if m.request_id == "r2")
    assert r2.error_code == "bad_request"


# -- WORKER-11 / TAGSCRIPT-21: through the supervisor, as a process ---------------------------


@pytest.fixture
async def previews(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PreviewSupervisor]:
    monkeypatch.setenv("RIFFERR_TEST_HEARTBEAT", "0.2")
    create_profile(config_root(), "main")
    sup = PreviewSupervisor(
        command=lambda p: [sys.executable, FAKE_WORKER, "--profile", p, "--read-only"],
        hung_after=3.0,
        stop_grace=1.0,
        watchdog_interval=0.1,
    )
    yield sup
    await sup.shutdown()


def request(text: str = EP) -> dict[str, Any]:
    return {"kind": "tagscript.preview", "payload": {"script_text": text}}


async def test_WORKER_11_preview_through_a_read_only_worker(previews: PreviewSupervisor) -> None:
    result = await previews.request("main", request(), timeout=10)
    r = PreviewResponse.model_validate(result)
    assert r.albums == 3
    assert previews.worker_pid("main") is not None


async def test_WORKER_11_worker_errors_come_back(previews: PreviewSupervisor) -> None:
    with pytest.raises(PreviewFailed) as exc:
        await previews.request("main", {"kind": "nope"}, timeout=10)
    assert exc.value.code == "bad_request"


async def test_TAGSCRIPT_21_timeout(
    previews: PreviewSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "0.5")
    monkeypatch.setenv("RIFFERR_TEST_ALBUMS", "20")
    with pytest.raises(PreviewTimeout):
        await previews.request("main", request(), timeout=0.5)
    # the worker cancels it and answers the next one (no templates, so no delay)
    result = await previews.request("main", request("actions:\n  - delete: [x]\n"), timeout=10)
    assert PreviewResponse.model_validate(result).albums == 20


async def test_TAGSCRIPT_21_newer_preview_supersedes_older(
    previews: PreviewSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "0.2")
    monkeypatch.setenv("RIFFERR_TEST_ALBUMS", "10")
    first = asyncio.create_task(previews.request("main", request(), timeout=30, client="tab"))
    await asyncio.sleep(1.0)
    second = asyncio.create_task(
        previews.request("main", request("actions:\n  - delete: [x]\n"), timeout=30, client="tab")
    )
    with pytest.raises(PreviewSuperseded):
        await first
    r = PreviewResponse.model_validate(await second)
    assert r.albums == 10


async def test_WORKER_11_crash_fails_pending_requests(
    previews: PreviewSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIFFERR_TEST_EVAL_DELAY", "1")
    monkeypatch.setenv("RIFFERR_TEST_ALBUMS", "10")
    task = asyncio.create_task(previews.request("main", request(), timeout=30))
    while previews.worker_pid("main") is None:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.5)
    pid = previews.worker_pid("main")
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    with pytest.raises(PreviewUnavailable):
        await task


async def test_WORKER_11_failed_start_is_unavailable(
    previews: PreviewSupervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RIFFERR_TEST_WRITABLE", "1")
    with pytest.raises(PreviewUnavailable, match="read-only"):
        await previews.request("main", request(), timeout=10)


def test_WORKER_11_default_command_starts_a_read_only_worker() -> None:
    assert PreviewSupervisor.default_command("p")[-3:] == ["--profile", "p", "--read-only"]
