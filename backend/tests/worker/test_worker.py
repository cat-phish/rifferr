"""The worker loop in-process (no subprocess): commands, profiles, idle exit."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from rifferr.appdb import AppDB
from rifferr.backends.fake import FakeBackend
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, create_profile, load_profile
from rifferr.worker.process import Commands, Worker
from rifferr.worker.protocol import WorkerCommand, WorkerMessage
from tests.engine.steps import ALL
from tests.worker.conftest import ECHO, SLOW


class Harness:
    def __init__(self, tmp_path: Path, idle_timeout: float | None = None) -> None:
        create_profile(config_root(), "main")
        self.db = AppDB(tmp_path / "app.db")
        self.sent: list[WorkerMessage] = []
        self.queue: Commands = asyncio.Queue()
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry(ALL),
            FakeBackend(),
            self.sent.append,
            idle_timeout=idle_timeout,
            cancel_grace=0.2,
        )

    def submit(self, source: str) -> str:
        payload = {"source": source, "source_name": "wf.yaml", "vars": {}, "trusted": False}
        return self.db.submit("main", payload).run_id  # type: ignore[arg-type]

    def events(self, run_id: str) -> list[str]:
        return [
            m.event.type
            for m in self.sent
            if m.kind == "event" and m.event is not None and m.event.run_id == run_id
        ]

    async def until(self, run_id: str, type: str) -> None:
        async def poll() -> None:
            while type not in self.events(run_id):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 5)


async def test_WORKER_2_refuses_commands_for_another_profile(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    task = asyncio.create_task(h.worker.serve(h.queue))
    h.queue.put_nowait(WorkerCommand(kind="stop", profile="other"))
    await asyncio.sleep(0.05)
    assert not task.done()  # a stop for another profile is refused too
    errors = [m.message for m in h.sent if m.kind == "error"]
    assert errors == ["worker for profile 'main' refused a 'stop' command for profile 'other'"]
    h.queue.put_nowait(WorkerCommand(kind="stop", profile="main"))
    await asyncio.wait_for(task, 2)


async def test_WORKER_3_claims_queued_jobs_on_start_and_on_wake(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.submit(ECHO)
    task = asyncio.create_task(h.worker.serve(h.queue))
    await h.until(first, "run.finished")
    assert h.sent[0].kind == "ready"
    second = h.submit(ECHO)
    h.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))
    await h.until(second, "run.finished")
    h.queue.put_nowait(None)  # server gone: stdin EOF
    await asyncio.wait_for(task, 2)
    assert [j.state for j in h.db.jobs("main")] == ["succeeded", "succeeded"]


async def test_WORKER_3_invalid_workflow_fails_the_job(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.submit("name: t\nsteps:\n  - {id: a, uses: nope}\n")
    task = asyncio.create_task(h.worker.serve(h.queue))
    await h.until(run_id, "run.finished")
    job = h.db.job_for_run(run_id)
    assert job is not None
    assert job.state == "failed"
    assert "unknown step type 'nope'" in (job.error or "")
    h.queue.put_nowait(None)
    await task


async def test_WORKER_5_cancel_only_matching_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.submit(SLOW)
    task = asyncio.create_task(h.worker.serve(h.queue))
    await h.until(run_id, "run.step.started")
    h.queue.put_nowait(WorkerCommand(kind="cancel_run", profile="main", run_id="someone-else"))
    await asyncio.sleep(0.05)
    assert "run.finished" not in h.events(run_id)
    h.queue.put_nowait(WorkerCommand(kind="cancel_run", profile="main", run_id=run_id))
    await h.until(run_id, "run.finished")
    job = h.db.job_for_run(run_id)
    assert job is not None
    assert job.state == "cancelled"
    h.queue.put_nowait(None)
    await task


async def test_WORKER_7_idle_timeout_ends_serve(tmp_path: Path) -> None:
    h = Harness(tmp_path, idle_timeout=0.2)
    run_id = h.submit(ECHO)
    await asyncio.wait_for(h.worker.serve(h.queue), 3)
    assert "run.finished" in h.events(run_id)


async def test_WORKER_7_no_idle_timeout_keeps_serving(tmp_path: Path) -> None:
    h = Harness(tmp_path, idle_timeout=None)
    task = asyncio.create_task(h.worker.serve(h.queue))
    await asyncio.sleep(0.5)
    assert not task.done()
    h.queue.put_nowait(WorkerCommand(kind="stop", profile="main"))
    await asyncio.wait_for(task, 2)


async def test_WORKER_8_stop_cancels_the_current_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.submit(SLOW)
    queued = h.submit(ECHO)
    task = asyncio.create_task(h.worker.serve(h.queue))
    await h.until(run_id, "run.step.started")
    h.queue.put_nowait(WorkerCommand(kind="stop", profile="main"))
    await asyncio.wait_for(task, 3)
    job = h.db.job_for_run(run_id)
    assert job is not None
    assert job.state == "cancelled"
    still = h.db.job_for_run(queued)
    assert still is not None
    assert still.state == "queued"  # left for the next worker


@pytest.mark.parametrize(
    ("text", "expected"),
    [("", 600), ("worker: { idle_timeout: null }\n", None), ("worker: { idle_timeout: 30 }\n", 30)],
)
def test_WORKER_7_idle_timeout_profile_setting(text: str, expected: int | None) -> None:
    from rifferr.core.profile import parse_profile

    p = parse_profile(text or "{}\n", "p.yaml", name="p", root=Path("/p"), env={})
    assert p.worker.idle_timeout == expected


def test_WORKER_7_idle_timeout_must_be_positive() -> None:
    from rifferr.core.profile import ProfileLoadError, parse_profile

    with pytest.raises(ProfileLoadError, match="idle_timeout"):
        parse_profile("worker: { idle_timeout: 0 }\n", "p.yaml", name="p", root=Path("/p"), env={})


def test_WORKER_9_command_lines_split_across_reads() -> None:
    import os

    from rifferr.worker.process import _lines  # pyright: ignore[reportPrivateUsage]

    r, w = os.pipe()
    os.write(w, b'{"a":1}\n{"b":\n2}\n\ntail')
    os.close(w)
    try:
        assert list(_lines(r, chunk=3)) == [b'{"a":1}', b'{"b":', b"2}", b"", b"tail"]
    finally:
        os.close(r)
