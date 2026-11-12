from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from rifferr.api.app import ApiSettings, create_api
from rifferr.api.security import secure
from rifferr.api.state import ServerState
from rifferr.profile_store import WORKFLOWS_DIR, config_root, create_profile, profile_dir
from tests.worker.fake_worker import registry

FAKE_WORKER = str(Path(__file__).parents[1] / "worker" / "fake_worker.py")
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"


def wf(*steps: str) -> str:
    return "name: t\nsteps:\n" + "".join(f"  - {s}\n" for s in steps)


ECHO = wf(
    "{id: a, uses: test.echo, with: {message: hi}}",
    "{id: b, uses: test.echo, with: {message: bye}}",
)
SLOW = wf("{id: a, uses: test.slow, with: {cooperative: true}}")


def settings(tmp_path: Path, **kw: Any) -> ApiSettings:
    db = tmp_path / "app.db"
    defaults: dict[str, Any] = {
        "port": PORT,
        "db_path": db,
        "frontend_dir": None,
        "worker_command": lambda p: [sys.executable, FAKE_WORKER, "--profile", p, "--db", str(db)],
        "preview_command": lambda p: [sys.executable, FAKE_WORKER, "--profile", p, "--read-only"],
        "registry": registry(),
        "keepalive": 0.2,
        "write_token": False,
    }
    return ApiSettings(**{**defaults, **kw})


@dataclass
class Api:
    app: FastAPI
    settings: ApiSettings
    client: httpx.AsyncClient

    @property
    def state(self) -> ServerState:
        return self.app.state.rifferr

    def add_workflow(
        self, name: str, text: str, profile: str = "main", suffix: str = ".yaml"
    ) -> Path:
        path = profile_dir(config_root(), profile) / WORKFLOWS_DIR / f"{name}{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    async def start(self, workflow: str, profile: str = "main", **vars: str) -> str:
        r = await self.client.post(
            "/api/runs", json={"profile": profile, "workflow": workflow, "vars": vars}
        )
        assert r.status_code == 201, r.text
        return str(r.json()["id"])

    async def wait_finished(self, run_id: str, timeout: float = 15) -> dict[str, Any]:
        async def poll() -> dict[str, Any]:
            while True:
                r = await self.client.get(f"/api/runs/{run_id}")
                body: dict[str, Any] = r.json()
                if body["state"] in ("succeeded", "failed", "cancelled"):
                    return body
                await asyncio.sleep(0.05)

        return await asyncio.wait_for(poll(), timeout)


def auth_client(app: Any, token: str, base: str = BASE) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=base,
        headers={"authorization": f"Bearer {token}", "origin": base},
    )


@pytest.fixture
async def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Api]:
    monkeypatch.setenv("RIFFERR_TEST_HEARTBEAT", "0.2")
    create_profile(config_root(), "main")
    s = settings(tmp_path)
    app = create_api(s)
    async with (
        app.router.lifespan_context(app),
        auth_client(secure(app, s.security()), s.token) as client,
    ):
        yield Api(app, s, client)


def parse_sse(text: str) -> list[dict[str, Any]]:
    """SSE messages (comments dropped) as {id, event, data}."""
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        msg: dict[str, Any] = {}
        for line in block.splitlines():
            if not line or line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            msg[key] = json.loads(value) if key == "data" else value
        if msg:
            out.append(msg)
    return out
