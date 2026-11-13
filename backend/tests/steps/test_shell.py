from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rifferr.core.profile import Secret, SecretRef
from rifferr.core.workflow import load_workflow, validate
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import RunState, StepTypeRegistry
from rifferr.steps import process, shell
from tests.steps.conftest import RUN_ID, ShellHarness

PY = sys.executable
PRINT_ARGS = "import json, sys; print(json.dumps(sys.argv[1:]))"
PRINT_ENV = "import json, os; print(json.dumps(dict(os.environ)))"


def step(with_: dict[str, Any], *, id: str = "s", extra: str = "") -> str:
    """A one-step workflow; ``with`` is written as JSON (valid YAML flow)."""
    return f"name: t\nsteps:\n  - id: {id}\n    uses: shell\n{extra}    with: {json.dumps(with_)}\n"


def errors(src: str) -> list[str]:
    wf, doc = load_workflow(textwrap.dedent(src), "wf.yaml")
    return [str(e) for e in validate(wf, StepTypeRegistry([shell.ShellStep()]), doc=doc)]


def statuses(run: Any) -> list[str]:
    return [s.status for s in run.steps]


# -- SHELL-1: argv list or shell string ---------------------------------------


def test_SHELL_1_string_without_shell_true_is_a_validation_error() -> None:
    errs = errors(step({"run": "ls -la"}))
    assert len(errs) == 1
    assert "needs 'shell: true'" in errs[0]


def test_SHELL_1_list_with_shell_true_is_a_validation_error() -> None:
    assert any("single command string" in e for e in errors(step({"run": ["ls"], "shell": True})))


def test_SHELL_1_empty_command_is_a_validation_error() -> None:
    assert any("must not be empty" in e for e in errors(step({"run": []})))


async def test_SHELL_1_shell_string_runs_through_sh(sh: ShellHarness) -> None:
    run = await sh.run(
        step({"run": "echo one; echo two | tr a-z A-Z", "shell": True, "per": "run"})
    )
    assert run.state is RunState.succeeded
    assert sh.lines(run) == ["one", "TWO"]


async def test_SHELL_1_shell_string_is_not_template_evaluated(sh: ShellHarness) -> None:
    sh.album("A", title="x; echo injected")
    run = await sh.run(step({"run": "echo \"$RIFFERR_ALBUM_DIR\" '$album'", "shell": True}))
    assert sh.lines(run) == [f"{sh.tmp}/music/A $album"]


# -- SHELL-2: templating without word splitting ------------------------------


async def test_SHELL_2_templates_never_split_words(sh: ShellHarness) -> None:
    sh.album("First EP", comment="a b; rm -rf /")
    run = await sh.run(step({"run": [PY, "-c", PRINT_ARGS, "$album", "$comment", "$$x"]}))
    assert run.state is RunState.succeeded
    assert json.loads(sh.lines(run)[0]) == ["First EP", "a b; rm -rf /", "$x"]


async def test_SHELL_2_workflow_interpolation_before_templates(sh: ShellHarness) -> None:
    sh.album("A")
    src = step({"run": [PY, "-c", PRINT_ARGS, "{{ vars.prefix }}-$album", "{{ run.id }}"]})
    run = await sh.run(src, vars={"prefix": "p"})
    assert json.loads(sh.lines(run)[0]) == ["p-A", RUN_ID]


async def test_SHELL_2_template_error_fails_the_target(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step({"run": ["echo", "%upper{$album}"]}))
    assert run.state is RunState.failed
    assert "template error" in (run.steps[0].error or "")


# -- SHELL-3: per run / album / item -----------------------------------------


async def test_SHELL_3_per_album_runs_once_per_album_in_order(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    sh.album("C", genre="skip")
    src = step({"run": ["echo", "$album"]}, extra="    when: ^genre:skip\n")
    run = await sh.run(src)
    assert sh.lines(run) == ["A", "B"]
    targets = [d["target"] for d in sh.logs(run, "stdout")]
    assert targets == ["album-1", "album-2"]


async def test_SHELL_3_per_item_runs_once_per_item(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "two", "track": 2}, {"title": "one", "track": 1}])
    run = await sh.run(step({"run": ["echo", "$title"], "per": "item"}))
    assert sh.lines(run) == ["one", "two"]


async def test_SHELL_3_per_run_runs_once_without_templates(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    run = await sh.run(step({"run": ["echo", "$album"], "per": "run"}))
    assert sh.lines(run) == ["$album"]
    assert "target" not in sh.logs(run, "stdout")[0]


async def test_SHELL_3_default_per_is_album() -> None:
    assert shell.ShellParams(run=["ls"]).per == "album"


async def test_SHELL_3_no_albums_runs_nothing(sh: ShellHarness) -> None:
    run = await sh.run(step({"run": ["echo", "hi"]}))
    assert run.state is RunState.succeeded
    assert sh.lines(run) == []


# -- SHELL-4: environment ------------------------------------------------------


async def test_SHELL_4_environment(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t", "track": 1}])
    src = step({"run": [PY, "-c", PRINT_ENV], "per": "item", "env": {"EXTRA": "$title"}})
    run = await sh.run(src, vars={"music-dir": "/m"})
    env = json.loads(sh.lines(run)[0])
    assert env["RIFFERR_RUN_ID"] == RUN_ID
    assert env["RIFFERR_PROFILE"] == "main"
    assert env["RIFFERR_ALBUM_DIR"] == f"{sh.tmp}/music/A"
    assert env["RIFFERR_ITEM_PATH"] == f"{sh.tmp}/music/A/01 t.flac"
    assert env["RIFFERR_VAR_MUSIC_DIR"] == "/m"
    assert env["BEETSDIR"] == "/beets"  # backend.command_env()
    assert env["EXTRA"] == "t"
    assert env["PATH"] == "/usr/bin:/bin"


async def test_SHELL_4_per_album_has_no_item_path(sh: ShellHarness) -> None:
    sh.album("A")
    env = json.loads(sh.lines(await sh.run(step({"run": [PY, "-c", PRINT_ENV]})))[0])
    assert "RIFFERR_ITEM_PATH" not in env
    assert env["RIFFERR_ALBUM_DIR"] == f"{sh.tmp}/music/A"


async def test_SHELL_4_inherited_secret_env_vars_are_removed(sh: ShellHarness) -> None:
    sh.environ["DISCOGS_TOKEN"] = "tok-123456"
    sh.environ["OTHER"] = "kept"
    secrets = ProfileSecrets(
        {"discogs": SecretRef.parse("env:DISCOGS_TOKEN")}, SecretResolver(env=sh.environ)
    )
    run = await sh.run(step({"run": [PY, "-c", PRINT_ENV], "per": "run"}), secrets=secrets)
    env = json.loads(sh.lines(run)[0])
    assert "DISCOGS_TOKEN" not in env
    assert "RIFFERR_SECRET_DISCOGS" not in env
    assert env["OTHER"] == "kept"


async def test_SHELL_4_listed_secret_is_exposed_and_redacted(sh: ShellHarness) -> None:
    sh.environ["DISCOGS_TOKEN"] = "tok-123456"
    secrets = ProfileSecrets(
        {
            "discogs": SecretRef.parse("env:DISCOGS_TOKEN"),
            "api-key": SecretRef.parse("keyring:svc/key"),
        },
        SecretResolver(env=sh.environ, keyring_get=lambda s, n: "key-abcdef"),
    )
    code = "import os; print(os.environ['RIFFERR_SECRET_API_KEY'], os.environ['DISCOGS_TOKEN'])"
    src = step({"run": [PY, "-c", code], "per": "run", "secrets": ["api-key", "discogs"]})
    run = await sh.run(src, secrets=secrets)
    assert run.state is RunState.succeeded
    assert sh.lines(run) == ["*** ***"]
    raw = json.dumps([e.data for e in sh.sink.since(run.id, 0)])
    assert "key-abcdef" not in raw
    assert "tok-123456" not in raw


async def test_SHELL_4_unknown_secret_fails_the_step(sh: ShellHarness) -> None:
    run = await sh.run(step({"run": ["true"], "per": "run", "secrets": ["nope"]}))
    assert run.state is RunState.failed
    assert "secret 'nope' is not available" in (run.steps[0].error or "")


# -- SHELL-5: stdin snapshot ---------------------------------------------------

READ_STDIN = "import sys; print(sys.stdin.read())"


async def test_SHELL_5_album_snapshot_on_stdin(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t", "track": 1}], year=2001)
    run = await sh.run(step({"run": [PY, "-c", READ_STDIN]}))
    data = json.loads(sh.lines(run)[0])
    [album] = sh.backend.query(sh.engine.runs[run.id] and _sel())
    assert data == album.model_dump(mode="json")


async def test_SHELL_5_item_snapshot_on_stdin(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t", "track": 1}])
    run = await sh.run(step({"run": [PY, "-c", READ_STDIN], "per": "item"}))
    data = json.loads(sh.lines(run)[0])
    # Items carry their album's fields, as beets items do (C-CHANGESET CHANGESET-12).
    assert {"title": "t", "track": 1, "album": "A"}.items() <= data["fields"].items()
    assert data["path"].endswith("01 t.flac")


async def test_SHELL_5_run_gets_all_albums(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    run = await sh.run(step({"run": [PY, "-c", READ_STDIN], "per": "run"}))
    data = json.loads(sh.lines(run)[0])
    assert [a["fields"]["album"] for a in data["albums"]] == ["A", "B"]


async def test_SHELL_5_command_ignoring_stdin_is_fine(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "x" * 10, "track": n} for n in range(1, 2000)])
    run = await sh.run(step({"run": ["true"]}))
    assert run.state is RunState.succeeded


def _sel() -> Any:
    from rifferr.core.selection import Selection

    return Selection(run_id=RUN_ID)


# -- SHELL-6: streaming logs ---------------------------------------------------


async def test_SHELL_6_stdout_and_stderr_stream_with_target(sh: ShellHarness) -> None:
    sh.album("A")
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.stdout.write('tail')"
    run = await sh.run(step({"run": [PY, "-c", code]}))
    assert sh.logs(run, "stdout") == [
        {"stream": "stdout", "line": "out", "target": "album-1"},
        {"stream": "stdout", "line": "tail", "target": "album-1"},
    ]
    assert sh.logs(run, "stderr") == [{"stream": "stderr", "line": "err", "target": "album-1"}]


async def test_SHELL_6_lines_stream_before_exit(sh: ShellHarness) -> None:
    seen: list[str] = []
    done = asyncio.Event()

    def listen(event: Any) -> None:
        if event.type == "run.step.log" and event.data["stream"] == "stdout":
            seen.append(event.data["line"])
            done.set()

    sh.engine.subscribe(listen)
    code = "import sys, time; print('early', flush=True); time.sleep(30)"
    run = sh.create(step({"run": [PY, "-c", code], "per": "run"}))
    task = asyncio.create_task(sh.engine.execute(run))
    await asyncio.wait_for(done.wait(), 10)
    assert seen == ["early"]
    sh.engine.cancel(run.id)
    await task


async def test_SHELL_6_output_is_redacted(sh: ShellHarness) -> None:
    from rifferr.core.profile import Redactor

    run = await sh.run(
        step({"run": ["echo", "token=hunter22"], "per": "run"}),
        redactor=Redactor([Secret("hunter22")]),
    )
    assert sh.lines(run) == ["token=***"]


async def test_SHELL_6_long_lines_are_split(sh: ShellHarness) -> None:
    code = f"print('x' * {process.MAX_LINE * 2 + 5})"
    run = await sh.run(step({"run": [PY, "-c", code], "per": "run"}))
    assert [len(line) for line in sh.lines(run)] == [process.MAX_LINE, process.MAX_LINE, 5]


# -- SHELL-7, SHELL-9: timeout and cancellation --------------------------------

# A parent that ignores SIGTERM, with a background child; both write their pids.
STUBBORN = """
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(os.getpid(), child.pid, flush=True)
time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); check its state.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True  # no /proc (macOS): kill(0) succeeded
    except ProcessLookupError:
        return False
    return stat.split(") ", 1)[1][0] != "Z"


async def _wait_dead(pids: list[int]) -> bool:
    for _ in range(100):
        if not any(_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.02)
    return False


async def test_SHELL_7_timeout_kills_the_process_group(
    sh: ShellHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "KILL_GRACE", 0.2)
    sh.album("A")
    run = await sh.run(step({"run": [PY, "-c", STUBBORN], "timeout": 1}))
    assert run.state is RunState.failed
    assert run.steps[0].failed_targets[0].message == "timed out after 1 s"
    pids = [int(p) for p in sh.lines(run)[0].split()]
    assert await _wait_dead(pids)


async def test_SHELL_7_sigterm_comes_first(sh: ShellHarness) -> None:
    code = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *a: (print('got TERM', flush=True), sys.exit(3)))\n"
        "print('ready', flush=True); time.sleep(60)\n"
    )
    run = await sh.run(step({"run": [PY, "-c", code], "per": "run", "timeout": 1}))
    assert sh.lines(run) == ["ready", "got TERM"]
    assert run.steps[0].error == "timed out after 1 s"


async def test_SHELL_9_cancel_kills_the_process_group(
    sh: ShellHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "KILL_GRACE", 0.2)
    sh.album("A")
    sh.album("B")
    started = asyncio.Event()
    sh.engine.subscribe(lambda e: started.set() if e.type == "run.step.log" else None)
    run = sh.create(step({"run": [PY, "-c", STUBBORN]}))
    task = asyncio.create_task(sh.engine.execute(run))
    await asyncio.wait_for(started.wait(), 10)
    sh.engine.cancel(run.id)
    await asyncio.wait_for(task, 5)
    assert run.state is RunState.cancelled
    assert statuses(run) == ["cancelled"]
    assert len(sh.lines(run)) == 1  # album B never started
    pids = [int(p) for p in sh.lines(run)[0].split()]
    assert await _wait_dead(pids)


async def test_SHELL_9_hard_cancel_after_grace_still_kills(
    sh: ShellHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The engine's grace (0.5 s) is shorter than KILL_GRACE: the task is cancelled
    # while waiting and must still SIGKILL the group.
    monkeypatch.setattr(process, "KILL_GRACE", 30)
    started = asyncio.Event()
    sh.engine.subscribe(lambda e: started.set() if e.type == "run.step.log" else None)
    run = sh.create(step({"run": [PY, "-c", STUBBORN], "per": "run"}))
    task = asyncio.create_task(sh.engine.execute(run))
    await asyncio.wait_for(started.wait(), 10)
    sh.engine.cancel(run.id)
    await asyncio.wait_for(task, 5)
    assert run.state is RunState.cancelled
    assert await _wait_dead([int(p) for p in sh.lines(run)[0].split()[:1]])


# -- SHELL-8: exit codes and on_error ------------------------------------------


async def test_SHELL_8_nonzero_exit_fails_and_stops(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    run = await sh.run(step({"run": ["sh", "-c", "echo $0; exit 4", "$album"]}))
    assert run.state is RunState.failed
    assert sh.lines(run) == ["A"]
    assert run.steps[0].error == "exited with status 4"


async def test_SHELL_8_on_error_continue_runs_remaining_targets(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    src = step(
        {"run": ["sh", "-c", 'echo $0; [ "$0" = B ]', "$album"]},
        extra="    on_error: continue\n",
    )
    run = await sh.run(src)
    assert sh.lines(run) == ["A", "B"]
    assert [(f.target, f.message) for f in run.steps[0].failed_targets] == [
        ("album-1", "exited with status 1")
    ]


async def test_SHELL_8_missing_program_fails_target(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step({"run": ["/nonexistent/prog"]}))
    assert run.state is RunState.failed
    assert (run.steps[0].error or "").startswith("could not start /nonexistent/prog")


async def test_SHELL_8_killed_by_signal(sh: ShellHarness) -> None:
    run = await sh.run(step({"run": ["sh", "-c", "kill -9 $$"], "per": "run"}))
    assert run.steps[0].error == "killed by signal SIGKILL"


# -- SHELL-10: cwd -------------------------------------------------------------


async def test_SHELL_10_cwd_defaults(sh: ShellHarness) -> None:
    a = sh.album("A")
    run = await sh.run(step({"run": ["pwd"]}))
    assert sh.lines(run) == [str(a)]
    run = await sh.run(step({"run": ["pwd"], "per": "run"}))
    assert sh.lines(run) == [str(sh.work_dir)]


async def test_SHELL_10_cwd_is_templated(sh: ShellHarness) -> None:
    (sh.tmp / "work" / "A").mkdir(parents=True)
    sh.album("A")
    run = await sh.run(step({"run": ["pwd"], "cwd": f"{sh.tmp}/work/$album"}))
    assert sh.lines(run) == [f"{sh.tmp}/work/A"]


async def test_SHELL_10_missing_cwd_fails_target(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step({"run": ["pwd"], "cwd": "/nonexistent/dir"}))
    assert run.steps[0].error == "working directory /nonexistent/dir does not exist"


# SHELL-11 (emits: none | changes) and SHELL-14 .. -19 are in test_shell_changes.py.


# -- SHELL-12: trust -----------------------------------------------------------


async def test_SHELL_12_untrusted_run_is_refused_whatever_on_error(sh: ShellHarness) -> None:
    sh.album("A")
    marker = sh.tmp / "ran"
    src = (
        "name: t\nsteps:\n"
        f"  - {{id: a, uses: shell, on_error: continue, with: {{run: [touch, '{marker}']}}}}\n"
        f"  - {{id: b, uses: shell, with: {{run: [touch, '{marker}']}}}}\n"
    )
    run = await sh.run(src, trusted=False)
    assert run.state is RunState.failed
    assert statuses(run) == ["failed", "skipped"]
    assert "not trusted" in (run.steps[0].error or "")
    assert not marker.exists()


def test_SHELL_12_runs_are_untrusted_by_default(sh: ShellHarness) -> None:
    wf, doc = load_workflow(step({"run": ["ls"]}), "wf.yaml")
    run = sh.engine.create_run(
        wf, b"", profile_name="main", profile_vars={}, backend=sh.backend, doc=doc
    )
    assert run.trusted is False


# -- SHELL-13: platforms -------------------------------------------------------


async def test_SHELL_13_windows_is_refused(
    sh: ShellHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell.sys, "platform", "win32")
    run = await sh.run(step({"run": ["ls"], "per": "run"}))
    assert statuses(run) == ["failed"]
    assert "Linux or macOS" in (run.steps[0].error or "")


# -- registration --------------------------------------------------------------


def test_SHELL_1_registered_as_builtin_entry_point() -> None:
    from rifferr.steps.registry import load_step_types

    registry = load_step_types()
    assert "shell" in registry.names()
    assert registry.errors == []
