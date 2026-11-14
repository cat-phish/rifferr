"""S-BEETS-COMMAND: the ``beets.command`` step and BACKEND-14."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER, BackendCapabilities
from rifferr.backends.fake import FakeBackend
from rifferr.core.profile import Secret, SecretRef
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.core.workflow import load_workflow, safety_report, validate
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.steps import process
from rifferr.steps.beets_command import BeetsCommandParams, BeetsCommandStep
from tests.steps.conftest import RUN_ID, ShellHarness

PY = sys.executable
REGISTRY = StepTypeRegistry([BeetsCommandStep()])


def step(with_: dict[str, Any], *, extra: str = "") -> str:
    head = "name: t\nsteps:\n  - id: s\n    uses: beets.command\n"
    return f"{head}{extra}    with: {json.dumps(with_)}\n"


def errors(src: str) -> list[str]:
    wf, doc = load_workflow(src, "wf.yaml")
    return [str(e) for e in validate(wf, REGISTRY, doc=doc)]


class Harness(ShellHarness):
    def __init__(self, tmp_path: Path, environ: dict[str, str]) -> None:
        super().__init__(tmp_path, environ)
        self.engine.registry = StepTypeRegistry([BeetsCommandStep(environ=environ)])

    def argv(self, run: Run) -> list[str]:
        """What the default FakeBackend CLI printed: its arguments."""
        return json.loads(self.lines(run)[0])


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


# -- BEETS-COMMAND-1 to -3: parameters ----------------------------------------------


def test_BEETS_COMMAND_1_parameters_and_defaults() -> None:
    p = BeetsCommandParams.model_validate({"command": "move"})
    assert (p.command, p.args, p.timeout) == ("move", [], "1h")
    assert errors(step({"command": "replaygain", "args": ["-a"], "timeout": "2h"})) == []


@pytest.mark.parametrize(
    "with_",
    [
        {},
        {"command": "move -a"},
        {"command": ["move"]},
        {"command": "move", "args": "-a"},
        {"command": "Move"},
        {"command": "move", "timeout": "soon"},
        {"command": "move", "timeout": 60},
    ],
)
def test_BEETS_COMMAND_1_invalid_parameters(with_: dict[str, Any]) -> None:
    assert errors(step(with_)), with_


def test_BEETS_COMMAND_1_options_belong_in_args() -> None:
    assert any("options go in args" in e for e in errors(step({"command": "move -a"})))


@pytest.mark.parametrize("command", ["import", "config", "edit"])
def test_BEETS_COMMAND_3_refused_subcommands(command: str) -> None:
    (err,) = errors(step({"command": command}))
    assert "not allowed here" in err
    if command == "import":
        assert "beets.import" in err


async def test_BEETS_COMMAND_2_interpolation_but_no_beets_templates(h: Harness) -> None:
    h.album("A")
    src = (
        "name: t\nvars: {fmt: {default: '$albumartist'}}\nsteps:\n"
        "  - {id: s, uses: beets.command,"
        " with: {command: ls, args: ['-a', '-f', '{{ vars.fmt }} - $album']}}\n"
    )
    run = await h.run(src)
    assert run.state is RunState.succeeded
    assert h.argv(run)[:4] == ["ls", "-a", "-f", "$albumartist - $album"]


# -- BEETS-COMMAND-4 to -6: selection and argv ---------------------------------------


async def test_BEETS_COMMAND_4_query_is_the_run_marker_plus_when(h: Harness) -> None:
    h.album("A", format="FLAC")
    src = step(
        {"command": "replaygain", "args": ["-a"]},
        extra="    when: 'format:FLAC albumartist:Artist'\n",
    )
    run = await h.run(src)
    assert h.argv(run) == [
        "replaygain",
        "-a",
        f"{RUN_MARKER}::^{RUN_ID}$",
        "format:FLAC",
        "albumartist:Artist",
    ]


def test_BEETS_COMMAND_4_empty_selection_is_refused_by_the_backend() -> None:
    backend = FakeBackend()
    with pytest.raises(ValueError, match="whole library"):
        backend.cli_command("move", [], Selection())
    assert backend.cli_command("move", [], Selection(query="genre:x"))[-1] == "genre:x"


async def test_BEETS_COMMAND_5_nothing_selected_runs_nothing(h: Harness) -> None:
    h.backend.add_album({"album": "Old", RUN_MARKER: "another-run", "items": []})
    marker = h.tmp / "ran"
    h.backend.cli = [PY, "-c", f"open({str(marker)!r}, 'w').close()"]
    run = await h.run(step({"command": "move"}))
    assert run.state is RunState.succeeded
    assert not marker.exists()
    # LANES-3: a run without albums has no lanes, so the step has no pass at all.
    assert run.steps[0].status == "skipped"


async def test_BEETS_COMMAND_6_query_comes_last_and_is_never_split(h: Harness) -> None:
    h.album("A", genre="Hip Hop")
    src = step({"command": "move", "args": ["-d", "/x y"]}, extra="    when: 'genre:\"Hip Hop\"'\n")
    run = await h.run(src)
    argv = h.argv(run)
    assert argv == ["move", "-d", "/x y", f"{RUN_MARKER}::^{RUN_ID}$", "genre:Hip Hop"]


# -- BEETS-COMMAND-7: environment ----------------------------------------------------


async def test_BEETS_COMMAND_7_environment_stdin_and_cwd(h: Harness, monkeypatch: Any) -> None:
    h.album("A")
    script = (
        "import json, os, sys; print(json.dumps({'env': dict(os.environ),"
        " 'stdin': sys.stdin.read(), 'cwd': os.getcwd()}))"
    )
    h.backend.cli = [PY, "-c", script]
    h.environ["DISCOGS"] = "s3cret"
    h.environ["KEEP"] = "yes"
    secrets = ProfileSecrets(
        {"discogs": SecretRef.parse("env:DISCOGS")}, SecretResolver(env=h.environ)
    )
    run = await h.run(step({"command": "move"}), secrets=secrets)
    assert run.state is RunState.succeeded
    out = json.loads(h.lines(run)[0])
    env = out["env"]
    assert env["BEETSDIR"] == "/beets"
    assert (env["RIFFERR_RUN_ID"], env["RIFFERR_PROFILE"]) == (RUN_ID, "main")
    assert env["KEEP"] == "yes"
    assert "DISCOGS" not in env  # an inherited env: secret is removed
    assert not [k for k in env if k.startswith("RIFFERR_SECRET_")]
    assert out["stdin"] == ""
    assert Path(out["cwd"]) == h.work_dir


# -- BEETS-COMMAND-8 to -10: output, timeout, failures ---------------------------------


async def test_BEETS_COMMAND_8_output_streams_with_redaction(h: Harness) -> None:
    h.album("A")
    h.backend.cli = [
        PY,
        "-c",
        "import sys; print('moving token-xyz'); print('warn', file=sys.stderr)",
    ]
    from rifferr.core.profile import Redactor

    run = await h.run(step({"command": "move"}), redactor=Redactor([Secret("token-xyz")]))
    assert h.lines(run) == ["moving ***"]
    assert h.lines(run, "stderr") == ["warn"]


async def test_BEETS_COMMAND_9_timeout_stops_the_process_group(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "KILL_GRACE", 0.2)
    h.album("A")
    h.backend.cli = [PY, "-c", "import time; time.sleep(30)"]
    run = await h.run(step({"command": "move", "timeout": "1s"}))
    assert run.state is RunState.failed
    assert run.steps[0].error == "beet move timed out after 1s"


async def test_BEETS_COMMAND_9_cancellation_stops_the_process(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    monkeypatch.setattr(process, "KILL_GRACE", 0.2)
    h.album("A")
    h.backend.cli = [PY, "-c", "import time; print('started', flush=True); time.sleep(30)"]
    run = h.create(step({"command": "move"}))
    task = asyncio.create_task(h.engine.execute(run))
    while not h.lines(run):
        await asyncio.sleep(0.01)
    h.engine.cancel(run.id)
    await asyncio.wait_for(task, 5)
    assert run.state is RunState.cancelled


async def test_BEETS_COMMAND_10_non_zero_exit_fails_the_step(h: Harness) -> None:
    h.album("A")
    h.backend.cli = [PY, "-c", "import sys; sys.exit(3)"]
    run = await h.run(step({"command": "move"}, extra="    on_error: continue\n"))
    assert run.steps[0].status == "failed"
    assert run.steps[0].error == "beet move exited with status 3"
    assert run.state is RunState.succeeded  # continue behaves like skip (RUN-5)


# -- BEETS-COMMAND-12: what changed ---------------------------------------------------


class ChangingBackend(FakeBackend):
    """Applies ``pending`` to its albums once the CLI has written ``marker``, the way a
    real ``beet`` subprocess changes the library behind the worker's back."""

    def __init__(self, marker: Path, **kw: Any) -> None:
        super().__init__(env={"BEETSDIR": "/beets"}, **kw)
        self.marker = marker
        self.pending: dict[str, dict[str, Any]] = {}
        self.cli = [PY, "-c", f"open({str(marker)!r}, 'w').close()"]

    def query(self, selection: Selection) -> list[AlbumSnapshot]:
        if self.marker.exists() and self.pending:
            for album in self._albums:  # pyright: ignore[reportPrivateUsage]
                change = self.pending.pop(str(album.fields.get("album")), None)
                if change is None:
                    continue
                if "path" in change:
                    album.path = change.pop("path")
                album.fields.update(change)
                for item in album.items:
                    item.fields.update(change)
        return super().query(selection)


async def test_BEETS_COMMAND_12_logs_the_fields_each_album_changed(h: Harness) -> None:
    backend = ChangingBackend(h.tmp / "ran")
    h.backend = backend
    h.album("A")
    h.album("B")
    backend.pending = {"A": {"path": "/new/A", "replaygain_album_gain": -7.5, "mtime": 1.0}}
    run = await h.run(step({"command": "replaygain", "args": ["-a"]}))
    assert run.state is RunState.succeeded
    assert h.lines(run, "info") == [
        "Artist - A: path, replaygain_album_gain changed",
        "Artist - B: no field changes",
    ]
    assert run.steps[0].outputs == {"albums": 2, "changed": 1}


# -- BEETS-COMMAND-13, -14: trust and capabilities ---------------------------------------


async def test_BEETS_COMMAND_13_untrusted_run_is_refused(h: Harness) -> None:
    h.album("A")
    marker = h.tmp / "ran"
    h.backend.cli = [PY, "-c", f"open({str(marker)!r}, 'w').close()"]
    run = await h.run(step({"command": "move"}, extra="    on_error: continue\n"), trusted=False)
    assert run.state is RunState.failed  # refusals stop the run whatever on_error says
    assert "not trusted" in (run.steps[0].error or "")
    assert not marker.exists()


def test_BEETS_COMMAND_13_safety_report_lists_the_command() -> None:
    src = (
        "name: t\nsteps:\n"
        "  - {id: m, uses: beets.command, with: {command: move, args: ['-a']}}\n"
        "  - {id: r, uses: beets.command, with: {command: remove, args: ['-d', '-f']}}\n"
    )
    wf, _ = load_workflow(src, "wf.yaml")
    report = safety_report(wf)
    assert [(f.step_id, f.detail) for f in report.of_kind("command")] == [
        ("m", "beet move -a"),
        ("r", "beet remove -d -f"),
    ]
    assert [(f.step_id, f.detail) for f in report.of_kind("destructive")] == [("r", "beet remove")]


def test_BEETS_COMMAND_14_requires_query_and_cli() -> None:
    assert BeetsCommandStep.requires == BackendCapabilities(query=True, cli=True)
    wf, doc = load_workflow(step({"command": "move"}), "wf.yaml")
    caps = BackendCapabilities(query=True)
    assert any("cli" in str(e) for e in validate(wf, REGISTRY, caps, doc=doc))


def test_BACKEND_14_read_only_backends_have_no_cli() -> None:
    assert not FakeBackend().read_only_view().capabilities.cli
