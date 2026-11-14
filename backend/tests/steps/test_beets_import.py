"""beets.import through the engine, against a temp beets profile."""

from __future__ import annotations

import json
import sys
import textwrap
from typing import Any

import pytest

from rifferr.core.workflow import load_workflow, validate
from rifferr.engine import Engine, InMemoryEventSink, Run, RunState, StepTypeRegistry
from rifferr.steps.beets_import import BeetsImportStep
from rifferr.steps.shell import ShellStep
from tests.backends.beets.conftest import BeetsEnv

REGISTRY = StepTypeRegistry([BeetsImportStep(), ShellStep(environ={"PATH": "/usr/bin:/bin"})])
PRINT = "import json, os, sys; print(json.dumps([sys.argv[1], os.environ['RIFFERR_ALBUM_DIR']]))"


def errors(src: str) -> list[str]:
    wf, doc = load_workflow(textwrap.dedent(src), "wf.yaml")
    return [str(e) for e in validate(wf, REGISTRY, doc=doc)]


async def run(env: BeetsEnv, src: str, vars: dict[str, str] | None = None) -> tuple[Run, list[Any]]:
    backend = env.backend()
    sink = InMemoryEventSink()
    engine = Engine(REGISTRY, sink, cancel_grace=0.5)
    text = textwrap.dedent(src)
    wf, doc = load_workflow(text, "wf.yaml")
    r = engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars=vars or {},
        backend=backend,
        doc=doc,
        trusted=True,
        work_dir=env.root,
    )
    await engine.execute(r)
    return r, list(sink.since(r.id, 0))


TWO_STEP = f"""\
name: import-then-list
vars:
  inbox: {{ description: folder to import }}
steps:
  - id: import
    uses: beets.import
    with: {{ path: "{{{{ vars.inbox }}}}", set: {{ source: inbox }} }}
  - id: list
    uses: shell
    with: {{ run: [{json.dumps(sys.executable)}, -c, {json.dumps(PRINT)}, "$album ($source)"] }}
"""


async def test_BEETS_IMPORT_5_two_step_workflow_import_then_shell(beets_env: BeetsEnv) -> None:
    beets_env.inbox_album()
    r, events = await run(beets_env, TWO_STEP, {"inbox": str(beets_env.inbox)})
    assert r.state is RunState.succeeded
    assert r.steps[0].outputs == {"imported": 1, "skipped": 0, "duplicates": 0, "decisions": 0}
    [line] = [e.data["line"] for e in events if e.type == "run.step.log" and e.step_id == "list"]
    name, album_dir = json.loads(str(line))
    assert name == "Fixture Album (inbox)"
    assert album_dir.startswith(str(beets_env.music))


async def test_BEETS_IMPORT_6_and_7_decisions_logged_and_skips_evented(
    beets_env: BeetsEnv,
) -> None:
    beets_env.inbox_album(name="a")
    beets_env.inbox_album(name="b")  # same album twice: the second is a duplicate
    src = "name: t\nsteps:\n  - id: import\n    uses: beets.import\n"
    src += f"    with: {{ path: '{beets_env.inbox}', mode: quiet }}\n"
    r, events = await run(beets_env, src)
    assert r.state is RunState.succeeded
    assert r.steps[0].outputs == {"imported": 1, "skipped": 1, "duplicates": 1, "decisions": 0}
    logs = [str(e.data["line"]) for e in events if e.type == "run.step.log"]
    assert [line.split(" ", 1)[0] for line in logs] == ["duplicate", "skipped", "imported"]
    [skip] = [e for e in events if e.type == "run.step.import.skipped"]
    assert skip.data == {
        "path": str(beets_env.inbox / "b"),
        "reason": "duplicate of an album in the library",
    }


@pytest.mark.parametrize(
    ("make", "message"), [("missing", "does not exist"), ("file", "is not a folder")]
)
async def test_BEETS_IMPORT_3_path_must_be_a_folder(
    beets_env: BeetsEnv, make: str, message: str
) -> None:
    target = beets_env.root / "target"
    if make == "file":
        target.write_text("x")
    src = f"name: t\nsteps:\n  - {{id: i, uses: beets.import, with: {{path: '{target}'}}}}\n"
    r, _ = await run(beets_env, src)
    assert r.state is RunState.failed
    assert message in (r.steps[0].error or "")


def test_IMPORT_UI_1_modes_default_to_auto() -> None:
    from rifferr.steps.beets_import import BeetsImportParams

    assert BeetsImportParams(path="/x").mode == "auto"
    for mode in ("quiet", "auto", "interactive"):
        step = f"{{id: i, uses: beets.import, with: {{path: /x, mode: {mode}}}}}"
        assert errors(f"name: t\nsteps:\n  - {step}\n") == []


def test_IMPORT_UI_10_decision_timeout_needs_a_deciding_mode() -> None:
    step = "name: t\nsteps:\n  - {id: i, uses: beets.import, with: {path: /x, %s}}\n"
    assert errors(step % "decision_timeout: 7d, on_timeout: asis") == []
    [err] = errors(step % "mode: quiet, decision_timeout: 7d")
    assert "decision_timeout needs mode auto or interactive" in err
    [err] = errors(step % "on_timeout: asis")
    assert "on_timeout needs a decision_timeout" in err


def test_BEETS_IMPORT_4_only_allowlisted_options() -> None:
    step = "name: t\nsteps:\n  - {id: i, uses: beets.import, with: {path: /x, options: %s}}\n"
    ok = step % "{move: yes, quiet_fallback: asis}"
    assert errors(ok) == []
    bad = step % "{timid: yes}"
    [err] = errors(bad)
    assert "with.options.timid" in err


def test_BEETS_IMPORT_15_registered_entry_points() -> None:
    import importlib.metadata

    from rifferr.steps.registry import load_step_types

    assert "beets.import" in load_step_types().names()
    backends = {e.name: e.value for e in importlib.metadata.entry_points(group="rifferr.backends")}
    assert backends["beets"] == "rifferr.backends.beets:from_profile"
