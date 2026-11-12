from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rifferr.appdb import AppDB, SqliteEventSink
from rifferr.engine import Event
from rifferr.profile_store import config_root, create_profile, profile_dir
from rifferr.trust import workflow_hash
from rifferr.worker.supervisor import WorkerSupervisor

FAKE_WORKER = str(Path(__file__).with_name("fake_worker.py"))


def wf(*steps: str) -> str:
    return "name: t\nsteps:\n" + "".join(f"  - {s}\n" for s in steps)


ECHO = wf("{id: a, uses: test.echo, with: {message: hi}}")
SLOW = wf("{id: a, uses: test.slow, with: {cooperative: true}}")
PID = wf("{id: a, uses: test.pid}")


def block(seconds: float) -> str:
    return wf(f"{{id: a, uses: test.block, with: {{seconds: {seconds}}}}}")


class Sup:
    def __init__(self, tmp_path: Path) -> None:
        self.db = AppDB(tmp_path / "app.db")
        self.sink = SqliteEventSink(self.db)
        self.supervisor = WorkerSupervisor(
            self.db,
            self.sink,
            command=lambda p: [
                sys.executable,
                FAKE_WORKER,
                "--profile",
                p,
                "--db",
                str(self.db.path),
            ],
            hung_after=2.0,
            stop_grace=1.0,
            watchdog_interval=0.1,
        )

    async def submit(self, source: str, profile: str = "main") -> str:
        self.db.trust(workflow_hash(source), "wf.yaml", "test")
        return await self.supervisor.submit_run(profile, source, source_name="wf.yaml")

    def events(self, run_id: str) -> list[Event]:
        return self.db.events_since(run_id, 0)

    async def wait_for(self, run_id: str, type: str = "run.finished", timeout: float = 15) -> Event:
        async def poll() -> Event:
            while True:
                for e in self.events(run_id):
                    if e.type == type:
                        return e
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(poll(), timeout)

    async def finished(self, run_id: str) -> Event:
        return await self.wait_for(run_id)


def set_idle_timeout(profile: str, value: str) -> None:
    path = profile_dir(config_root(), profile) / "profile.yaml"
    path.write_text(path.read_text() + f"worker: {{ idle_timeout: {value} }}\n")


@pytest.fixture
async def sup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Sup]:
    monkeypatch.setenv("RIFFERR_TEST_HEARTBEAT", "0.2")
    create_profile(config_root(), "main")
    create_profile(config_root(), "other")
    s = Sup(tmp_path)
    yield s
    await s.supervisor.shutdown()
    s.db.close()
