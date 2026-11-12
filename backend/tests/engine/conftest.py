from __future__ import annotations

import textwrap
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rifferr.backends.fake import FakeBackend
from rifferr.core.profile import Redactor, Secret
from rifferr.core.workflow import load_workflow
from rifferr.engine import Engine, InMemoryEventSink, Run, StepTypeRegistry
from tests.engine.steps import ALL


class FixedClock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


class Harness:
    def __init__(self) -> None:
        self.sink = InMemoryEventSink()
        self.clock = FixedClock()
        self.ids = iter(f"01RUN{n:021d}" for n in range(1, 1000))
        self.engine = Engine(
            StepTypeRegistry(ALL),
            self.sink,
            clock=self.clock,
            new_id=lambda: next(self.ids),
            cancel_grace=0.1,
        )
        self.backend = FakeBackend.from_fixture({"albums": []})

    def run(
        self,
        src: str,
        *,
        profile_vars: dict[str, str] | None = None,
        run_vars: dict[str, str] | None = None,
        secrets: list[str] | None = None,
    ) -> Run:
        text = textwrap.dedent(src)
        wf, doc = load_workflow(text, "wf.yaml")
        return self.engine.create_run(
            wf,
            text.encode(),
            profile_name="main",
            profile_vars=profile_vars or {},
            backend=self.backend,
            run_vars=run_vars,
            doc=doc,
            redactor=Redactor([Secret(s) for s in secrets or []]),
        )

    def events(self, run: Run) -> list[Any]:
        return list(self.sink.since(run.id, 0))

    def types(self, run: Run) -> list[tuple[str, str | None]]:
        return [(e.type, e.step_id) for e in self.events(run)]

    def logs(self, run: Run, stream: str = "info") -> list[str]:
        return [
            str(e.data["line"])
            for e in self.events(run)
            if e.type == "run.step.log" and e.data["stream"] == stream
        ]


@pytest.fixture
def h() -> Harness:
    return Harness()
