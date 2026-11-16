# ruff: noqa: F811, E501, E741
"""Audit findings SEC-21..24 (boundary B1: interpolation, templates, shell, beets.command)."""

from __future__ import annotations

import json
import sys
import textwrap
from typing import Any

from rifferr.backends.beets import _run_term  # pyright: ignore[reportPrivateUsage]
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow
from rifferr.core.workflow.safety import safety_report
from rifferr.engine import Engine, InMemoryEventSink, RunState, StepTypeRegistry
from rifferr.engine.executor import RunValidationError
from rifferr.steps.beets_import import BeetsImportStep
from rifferr.steps.notify import NotifyStep
from rifferr.steps.shell import ShellStep
from tests.backends.beets.conftest import BeetsEnv, beets_env  # noqa: F401
from tests.steps.conftest import ShellHarness
from tests.steps.conftest import sh as sh
from tests.steps.test_notify import FakeTargets

PY = sys.executable


# -- SEC-21 -----------------------------------------------------------------------------

INJECT = "/in/a; echo $((6*7))"


async def _shell_run(
    sh: ShellHarness,
    src: str,
    *,
    run_input: str | None = None,
    run_vars: dict[str, str] | None = None,
) -> tuple[Any, list[str]]:
    text = textwrap.dedent(src)
    wf, doc = load_workflow(text, "wf.yaml")
    try:
        run = sh.engine.create_run(
            wf,
            text.encode(),
            profile_name="main",
            profile_vars={},
            backend=sh.backend,
            doc=doc,
            trusted=True,
            work_dir=sh.work_dir,
            run_input=run_input,
            run_vars=run_vars,
        )
    except RunValidationError:
        return None, []  # refused up front: the safe outcome
    await sh.engine.execute(run)
    return run, sh.lines(run)


async def test_SEC_21_run_input_is_not_shell_text(sh: ShellHarness) -> None:
    src = """\
    name: t
    steps:
      - id: s
        uses: shell
        with: {run: "printf %s {{ run.input }}", shell: true, per: run}
    """
    run, lines = await _shell_run(sh, src, run_input=INJECT)
    assert not any("42" in l for l in lines), "folder name was run as shell code"
    assert run is None or run.state is RunState.failed


async def test_SEC_21_run_var_is_not_shell_text(sh: ShellHarness) -> None:
    src = """\
    name: t
    vars: {x: {default: safe}}
    steps:
      - id: s
        uses: shell
        with: {run: "printf %s {{ vars.x }}", shell: true, per: run}
    """
    run, lines = await _shell_run(sh, src, run_vars={"x": INJECT})
    assert not any("42" in l for l in lines), "run var was run as shell code"
    assert run is None or run.state is RunState.failed


# -- SEC-22 -----------------------------------------------------------------------------

PRINT = "import sys; print(sys.argv[1])"
LITERAL = "/inbox/$genre"


def _two_step(second: str) -> str:
    return f"""\
name: t
vars:
  inbox: {{ description: folder }}
steps:
  - id: import
    uses: beets.import
    with: {{ path: "{{{{ vars.inbox }}}}", set: {{ genre: SECRETGENRE }} }}
{second}
"""


async def _beets_run(env: BeetsEnv, src: str, notify: FakeTargets | None = None) -> tuple[Any, Any]:
    env.inbox_album()
    registry = StepTypeRegistry(
        [BeetsImportStep(), ShellStep(environ={"PATH": "/usr/bin:/bin"}), NotifyStep()]
    )
    sink = InMemoryEventSink()
    engine = Engine(registry, sink, cancel_grace=0.5)
    wf, doc = load_workflow(src, "wf.yaml")
    run = engine.create_run(
        wf,
        src.encode(),
        profile_name="main",
        profile_vars={"inbox": str(env.inbox)},
        backend=env.backend(),
        doc=doc,
        trusted=True,
        work_dir=env.root,
        run_input=LITERAL,
        notify=notify,
    )
    await engine.execute(run)
    return run, list(sink.since(run.id, 0))


async def test_SEC_22_run_input_is_literal_in_shell_argv(beets_env: BeetsEnv) -> None:
    second = f"  - id: show\n    uses: shell\n    with: {{ run: [{json.dumps(PY)}, -c, {json.dumps(PRINT)}, '{{{{ run.input }}}}'] }}\n"
    run, events = await _beets_run(beets_env, _two_step(second))
    assert run.state is RunState.succeeded
    lines = [
        str(e.data["line"]) for e in events if e.type == "run.step.log" and e.step_id == "show"
    ]
    assert lines == [LITERAL], f"run.input was re-evaluated as a beets template: {lines}"


async def test_SEC_22_run_input_is_literal_in_notify(beets_env: BeetsEnv) -> None:
    targets = FakeTargets(["t"])
    second = "  - id: tell\n    uses: notify\n    with: { body: '{{ run.input }}', title: x, targets: [t], per: album }\n"
    run, _ = await _beets_run(beets_env, _two_step(second), notify=targets)
    assert run.state is RunState.succeeded, [s for s in run.steps]
    assert [n.body for _, n in targets.sent] == [LITERAL]


# -- SEC-23 -----------------------------------------------------------------------------


def test_SEC_23_safety_report_flags_leading_field_argv() -> None:
    text = (
        "name: t\nsteps:\n  - id: s\n    uses: shell\n"
        "    with: {run: [printf, '$album'], cwd: '/tmp/$albumartist'}\n"
    )
    wf, _ = load_workflow(text, "wf.yaml")
    extra = [f for f in safety_report(wf).findings if f.kind != "command"]
    details = " ".join(f.detail for f in extra)
    assert "$album" in details, f"no finding for the leading field argv: {extra}"
    assert "cwd" in details or "$albumartist" in details, f"no finding for cwd: {extra}"


# -- SEC-24 -----------------------------------------------------------------------------

RID = "01RUN000000000000000000001"


def test_SEC_24_cli_command_or_query_stays_in_run(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    try:
        argv = backend.cli_command(
            "modify", ["-y", "checked=1"], Selection(query="genre:Y , genre:X", run_id=RID)
        )
    except ValueError:
        return  # refusing a comma part is a safe outcome
    query = argv[argv.index("checked=1") + 1 :]
    groups: list[list[str]] = [[]]
    for part in query:
        groups[-1].append(part)
        if part.endswith(","):
            groups.append([])
    term = _run_term(RID)
    assert all(term in g for g in groups), f"an OR group escapes the run marker: {query}"


def test_SEC_24_cli_command_has_double_dash(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    argv = backend.cli_command("list", [], Selection(query="-genre:x", run_id=RID))
    assert "--" in argv
    assert argv.index("--") < argv.index("-genre:x")
