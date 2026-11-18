"""S-SUBWORKFLOW in the worker: the run uses the called texts stored with its job,
also when it resumes after a review gate inside the call (SUBWORKFLOW-9, -12)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.review import ReviewStep
from rifferr.steps.workflow_call import WorkflowCallStep
from rifferr.trust import workflow_hash
from rifferr.worker.process import Worker
from rifferr.worker.protocol import WorkerCommand
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import GATE, RUN, Harness, serving

CALLED = wf(
    "{id: say, uses: test.echo, with: {message: 'stored {{ vars.word }}'}}",
    GATE,
    "{id: after, uses: test.echo, with: {message: 'after: {{ steps.check.decision }}'}}",
).replace("steps:", "vars: {word: {}}\nsteps:")
CALLER = wf(
    "{id: sub, uses: workflow, with: {workflow: called.yaml, vars: {word: hi}}}",
    "{id: last, uses: test.echo, with: {message: last}}",
)


class CallHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), WorkflowCallStep()]),
            self.backend,
            self._send,
        )

    def submit_called(self, called: str) -> None:
        payload: dict[str, Any] = {
            "source": CALLER,
            "source_name": "caller.yaml",
            "vars": {},
            "called": [{"path": "called.yaml", "source": called, "sha256": workflow_hash(called)}],
        }
        self.db.submit("main", payload, run_id=RUN)
        self.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[CallHarness]:
    harness = CallHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


async def test_SUBWORKFLOW_12_worker_runs_stored_texts_and_resumes_with_them(
    h: CallHarness,
) -> None:
    # No called.yaml on disk: only the stored text exists.
    h.album("A")
    h.submit_called(CALLED)
    await h.until(lambda: h.state() == "waiting_review")
    assert "stored hi" in h.logs()
    gate = h.gate()
    assert gate.step_id == "sub/check"
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert h.logs()[-2:] == ["after: continue", "last"]
    resumed = [e for e in h.events("run.resumed")]
    assert [e.data["after"] for e in resumed] == ["sub/check"]
