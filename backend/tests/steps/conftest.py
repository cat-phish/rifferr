from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.profile import Redactor
from rifferr.core.workflow import load_workflow
from rifferr.engine import Engine, InMemoryEventSink, Run, SecretAccess, StepTypeRegistry
from rifferr.steps import shell
from tests.backends.beets.conftest import beets_env  # noqa: F401  (shared fixture)
from tests.engine.conftest import FixedClock

RUN_ID = "01RUN000000000000000000001"


class ShellHarness:
    def __init__(self, tmp_path: Path, environ: dict[str, str]) -> None:
        self.tmp = tmp_path
        self.environ = environ
        self.sink = InMemoryEventSink()
        ids = iter(f"01RUN{n:021d}" for n in range(1, 1000))
        self.engine = Engine(
            StepTypeRegistry([shell.ShellStep(environ=environ)]),
            self.sink,
            clock=FixedClock(),
            new_id=lambda: next(ids),
            cancel_grace=0.5,
        )
        self.backend = FakeBackend(env={"BEETSDIR": "/beets"})
        self.work_dir = tmp_path / "profile"
        self.work_dir.mkdir()

    def album(self, name: str, items: list[dict[str, Any]] | None = None, **fields: Any) -> Path:
        path = self.tmp / "music" / name
        path.mkdir(parents=True)
        spec = {"album": name, "albumartist": "Artist", RUN_MARKER: RUN_ID, **fields}
        self.backend.add_album({**spec, "path": str(path), "items": items or []})
        return path

    def create(
        self,
        src: str,
        *,
        trusted: bool = True,
        vars: dict[str, str] | None = None,
        secrets: SecretAccess | None = None,
        redactor: Redactor | None = None,
    ) -> Run:
        text = textwrap.dedent(src)
        wf, doc = load_workflow(text, "wf.yaml")
        return self.engine.create_run(
            wf,
            text.encode(),
            profile_name="main",
            profile_vars=vars or {},
            backend=self.backend,
            doc=doc,
            trusted=trusted,
            work_dir=self.work_dir,
            secrets=secrets,
            redactor=redactor,
        )

    async def run(self, src: str, **kwargs: Any) -> Run:
        run = self.create(src, **kwargs)
        await self.engine.execute(run)
        return run

    def logs(self, run: Run, stream: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(e.data)
            for e in self.sink.since(run.id, 0)
            if e.type == "run.step.log" and (stream is None or e.data["stream"] == stream)
        ]

    def lines(self, run: Run, stream: str = "stdout") -> list[str]:
        return [str(d["line"]) for d in self.logs(run, stream)]


@pytest.fixture
def sh(tmp_path: Path) -> ShellHarness:
    return ShellHarness(tmp_path, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
