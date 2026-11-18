"""S-SUBWORKFLOW in the engine: called steps run inside the caller's run."""

from __future__ import annotations

import textwrap

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.core.profile import Redactor
from rifferr.core.workflow import load_workflow, resolve_calls, stored_reader
from rifferr.engine import Engine, Run, RunState, StepTypeRegistry
from rifferr.steps.workflow_call import WorkflowCallStep
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = [*ALL, WorkflowCallStep()]

ECHO = """\
name: Echo
vars:
  word: {description: what to say}
  tail: {default: "!"}
outputs:
  said: "{{ steps.say.said }}"
steps:
  - id: say
    uses: test.echo
    with:
      message: "{{ vars.word }}{{ vars.tail }} {{ run.trigger }}"
      outputs: {said: "{{ vars.word }}"}
"""

CALLER = """\
name: Caller
steps:
  - id: first
    uses: test.echo
    with: {message: one, outputs: {n: 3}}
  - id: talk
    uses: workflow
    with: {workflow: echo.yaml, vars: {word: "got {{ steps.first.n }}"}}
  - id: last
    uses: test.echo
    with: {message: "heard {{ steps.talk.said }}"}
"""


class CallHarness(Harness):
    def __init__(self) -> None:
        super().__init__()
        self.engine = Engine(
            StepTypeRegistry(REGISTRY),
            self.sink,
            clock=self.clock,
            new_id=lambda: next(self.ids),
            cancel_grace=0.1,
        )

    def call_run(self, src: str, files: dict[str, str], engine: Engine | None = None) -> Run:
        text = textwrap.dedent(src)
        wf, doc = load_workflow(text, "caller.yaml")
        texts = {k: textwrap.dedent(v) for k, v in files.items()}
        calls = resolve_calls(wf, doc, stored_reader(texts), name="caller.yaml")
        return (engine or self.engine).create_run(
            wf,
            text.encode(),
            profile_name="main",
            profile_vars={},
            backend=self.backend,
            doc=doc,
            redactor=Redactor([]),
            calls=calls,
        )

    def album(self, run: Run, name: str, **fields: str) -> str:
        return self.backend.add_album(
            {"album": name, "albumartist": "A", RUN_MARKER: run.id, **fields, "items": [{}]}
        ).id


@pytest.fixture
def c() -> CallHarness:
    return CallHarness()


async def test_SUBWORKFLOW_3_vars_use_the_callers_earlier_outputs(c: CallHarness) -> None:
    run = c.call_run(CALLER, {"echo.yaml": ECHO})
    await c.engine.execute(run)
    assert run.state is RunState.succeeded
    assert c.logs(run) == ["one", "got 3! manual", "heard got 3"]


async def test_SUBWORKFLOW_6_events_and_records_use_expanded_ids(c: CallHarness) -> None:
    run = c.call_run(CALLER, {"echo.yaml": ECHO})
    assert [s.id for s in run.steps] == ["first", "talk/say", "talk", "last"]
    await c.engine.execute(run)
    started = {e.step_id: dict(e.data) for e in c.events(run) if e.type == "run.step.started"}
    assert "call_path" not in started["first"]
    assert (started["talk/say"]["call_path"], started["talk/say"]["workflow"]) == (
        ["talk"],
        "echo.yaml",
    )
    assert started["talk"]["uses"] == "workflow"


async def test_SUBWORKFLOW_11_outputs_resolve_after_the_called_steps(c: CallHarness) -> None:
    run = c.call_run(CALLER, {"echo.yaml": ECHO})
    await c.engine.execute(run)
    (end,) = [r for r in run.steps if r.id == "talk"]
    assert end.outputs == {"said": "got 3"}


async def test_SUBWORKFLOW_15_run_records_called_file_hashes(c: CallHarness) -> None:
    import hashlib

    run = c.call_run(CALLER, {"echo.yaml": ECHO})
    digest = hashlib.sha256(textwrap.dedent(ECHO).encode()).hexdigest()
    assert run.called_hashes == {"echo.yaml": digest}


SEEN = """\
name: Seen
steps:
  - id: look
    uses: test.seen
"""


async def test_SUBWORKFLOW_7_call_when_narrows_called_steps(c: CallHarness) -> None:
    caller = (
        "name: c\nsteps:\n  - {id: all, uses: test.seen}\n"
        "  - {id: rock, uses: workflow, when: 'genre:Rock', with: {workflow: seen.yaml}}\n"
    )
    run = c.call_run(caller, {"seen.yaml": SEEN})
    c.album(run, "X", genre="Rock")
    c.album(run, "Y", genre="Jazz")
    await c.engine.execute(run)
    assert c.logs(run) == ["seen: X,Y", "seen: X"]


HOLD = """\
name: Hold
steps:
  - id: comp
    uses: test.propose
    when: "album:C"
    with: {set: {label: L}}
  - id: look
    uses: test.seen
"""


async def test_SUBWORKFLOW_9_a_hold_inside_a_call_holds_only_its_lane(c: CallHarness) -> None:
    caller = (
        "name: c\nsteps:\n  - {id: t, uses: workflow, with: {workflow: hold.yaml}}\n"
        "  - {id: after, uses: test.seen}\n"
    )
    run = c.call_run(caller, {"hold.yaml": HOLD})
    for name in ("A", "B", "C"):
        c.album(run, name)
    await c.engine.execute(run)
    assert run.state is RunState.waiting_review
    assert run.suspended_after is not None
    assert c.logs(run) == ["seen: A,B", "seen: A,B"]
    lanes = {
        lane.label: (lane.state, lane.position) for lane in c.engine.lanes.load(run.id).values()
    }
    assert lanes["A - C"] == ("waiting", "t/look")
    for stored in c.engine.changesets.for_run(run.id, "proposed"):
        c.engine.changesets.set_state(stored.changeset.id, "applied", resolved_by="test")
    engine = Engine(
        StepTypeRegistry(REGISTRY),
        c.sink,
        clock=c.clock,
        new_id=lambda: run.id,
        journal=c.engine.journal,
        changesets=c.engine.changesets,
        gates=c.engine.gates,
        lanes=c.engine.lanes,
    )
    again = c.call_run(caller, {"hold.yaml": HOLD}, engine)
    again.last_seq = run.last_seq
    engine.restore_steps(again, {r.id: r for r in run.steps if r.passes})
    await engine.execute(again, resume_after=run.suspended_after)
    assert again.state is RunState.succeeded
    assert c.logs(run)[-2:] == ["seen: C", "seen: C"]
