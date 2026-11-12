from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from rifferr.engine import Event
from rifferr.profile_store import config_root, create_profile
from rifferr.trust import workflow_hash
from rifferr.worker.supervisor import WorkerSupervisor
from tests.worker.conftest import ECHO, FAKE_WORKER, PID, SLOW, Sup, block, set_idle_timeout


async def _pid_of(sup: Sup, profile: str = "main") -> int:
    run_id = await sup.submit(PID, profile)
    await sup.finished(run_id)
    [step] = [e for e in sup.events(run_id) if e.type == "run.step.finished"]
    outputs = step.data["outputs"]
    assert isinstance(outputs, dict)
    pid = outputs["pid"]
    assert isinstance(pid, int)
    return pid


# -- WORKER-1: one worker per profile, started on demand ---------------------


async def test_WORKER_1_started_on_demand_one_per_profile(sup: Sup) -> None:
    assert sup.supervisor.worker_pid("main") is None
    first = await _pid_of(sup)
    assert await _pid_of(sup) == first
    assert sup.supervisor.worker_pid("main") == first
    assert await _pid_of(sup, "other") != first


def test_WORKER_1_default_command_uses_the_servers_python(sup: Sup) -> None:
    supervisor = WorkerSupervisor(sup.db, sup.sink)
    assert supervisor.default_command("main") == [
        sys.executable,
        "-m",
        "rifferr.worker",
        "--profile",
        "main",
        "--db",
        str(sup.db.path),
    ]


def test_WORKER_1_server_side_never_imports_beets() -> None:
    code = (
        "import sys, rifferr.worker.supervisor, rifferr.appdb\n"
        "bad = [m for m in sys.modules if m == 'beets' or m.startswith(('beets.', "
        "'rifferr.backends.beets', 'rifferr.worker.process'))]\n"
        "print(bad)\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# -- WORKER-2: one profile per worker -------------------------------------------
# (refusing commands for another profile is tested in-process, test_worker.py)


async def test_WORKER_2_each_worker_loads_its_own_profile(sup: Sup) -> None:
    run_id = await sup.submit(wf_profile_name(), "other")
    await sup.finished(run_id)
    logs = [e.data["line"] for e in sup.events(run_id) if e.type == "run.step.log"]
    assert logs == ["other"]


def wf_profile_name() -> str:
    return "name: t\nsteps:\n  - {id: a, uses: test.echo, with: {message: '{{ profile.name }}'}}\n"


# -- WORKER-3: requests go through the job table --------------------------------


async def test_WORKER_3_job_is_written_before_the_worker_starts(sup: Sup) -> None:
    run_id = await sup.submit(ECHO)
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert job.payload["source"] == ECHO
    assert (await sup.finished(run_id)).data["status"] == "succeeded"
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert job.state == "succeeded"
    assert job.claimed_at is not None


async def test_WORKER_3_queued_request_survives_a_worker_crash(sup: Sup) -> None:
    first = await sup.submit(block(60))
    await sup.wait_for(first, "run.step.log")
    second = await sup.submit(ECHO)
    pid = sup.supervisor.worker_pid("main")
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    assert (await sup.finished(first)).data["reason"] == "worker_crashed"
    assert (await sup.finished(second)).data["status"] == "succeeded"
    assert sup.supervisor.worker_pid("main") not in (None, pid)


# -- WORKER-4: events persisted by the server before fan-out ---------------------


async def test_WORKER_4_events_are_persisted_before_listeners(sup: Sup) -> None:
    seen: list[tuple[int, bool]] = []

    def listen(event: Event) -> None:
        stored = [e.seq for e in sup.db.events_since(event.run_id, 0)]
        seen.append((event.seq, event.seq in stored))

    sup.supervisor.subscribe(listen)
    run_id = await sup.submit(ECHO)
    await sup.finished(run_id)
    types = [e.type for e in sup.events(run_id)]
    assert types == [
        "run.started",
        "run.step.started",
        "run.step.log",
        "run.step.finished",
        "run.finished",
    ]
    assert seen == [(n, True) for n in range(1, 6)]


# -- WORKER-5: cancellation --------------------------------------------------------


async def test_WORKER_5_cancel_a_running_run(sup: Sup) -> None:
    run_id = await sup.submit(SLOW)
    await sup.wait_for(run_id, "run.step.started")
    await sup.supervisor.cancel(run_id)
    assert (await sup.finished(run_id)).data["status"] == "cancelled"
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert job.state == "cancelled"


async def test_WORKER_5_cancel_a_queued_run(sup: Sup) -> None:
    first = await sup.submit(SLOW)
    second = await sup.submit(ECHO)
    await sup.supervisor.cancel(second)
    assert [e.type for e in sup.events(second)] == ["run.finished"]
    assert sup.events(second)[0].data == {"status": "cancelled"}
    await sup.supervisor.cancel(first)
    await sup.finished(first)
    job = sup.db.job_for_run(second)
    assert job is not None
    assert job.state == "cancelled"


# -- WORKER-6: crashes -----------------------------------------------------------


async def test_WORKER_6_crash_fails_the_run_and_next_request_gets_a_fresh_worker(
    sup: Sup,
) -> None:
    run_id = await sup.submit(block(60))
    await sup.wait_for(run_id, "run.step.log")
    pid = sup.supervisor.worker_pid("main")
    assert pid is not None
    os.kill(pid, signal.SIGKILL)
    finished = await sup.finished(run_id)
    assert finished.data["status"] == "failed"
    assert finished.data["reason"] == "worker_crashed"
    assert [e.seq for e in sup.events(run_id)] == list(range(1, finished.seq + 1))
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert (job.state, job.error) == ("failed", "worker_crashed")
    assert await _pid_of(sup) != pid


async def test_WORKER_6_worker_that_cannot_start_fails_the_queue(sup: Sup) -> None:
    run_id = await sup.submit(ECHO, "ghost")
    finished = await sup.finished(run_id)
    assert finished.data["reason"] == "worker_failed_to_start"
    assert "no profile 'ghost'" in str(finished.data["error"])
    assert sup.supervisor.worker_pid("ghost") is None


# -- WORKER-7: idle exit -------------------------------------------------------------


async def test_WORKER_7_idle_worker_exits_cleanly(sup: Sup) -> None:
    set_idle_timeout("main", "1")
    pid = await _pid_of(sup)
    await asyncio.wait_for(sup.supervisor.wait_idle_exit("main"), 10)
    assert sup.supervisor.worker_pid("main") is None
    assert await _pid_of(sup) != pid  # a new request starts a new worker


# -- WORKER-8: shutdown --------------------------------------------------------------


async def test_WORKER_8_shutdown_cancels_cooperatively(sup: Sup) -> None:
    run_id = await sup.submit(SLOW)
    await sup.wait_for(run_id, "run.step.started")
    await sup.supervisor.shutdown()
    assert (await sup.finished(run_id)).data["status"] == "cancelled"
    assert sup.supervisor.worker_pid("main") is None


async def test_WORKER_8_unresponsive_worker_is_killed_after_grace(sup: Sup) -> None:
    run_id = await sup.submit(block(60))
    await sup.wait_for(run_id, "run.step.log")
    pid = sup.supervisor.worker_pid("main")
    loop = asyncio.get_running_loop()
    started = loop.time()
    await sup.supervisor.shutdown()
    assert loop.time() - started < 5
    finished = await sup.finished(run_id)
    assert finished.data["reason"] == "server_shutdown"
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_WORKER_8_no_new_runs_after_shutdown(sup: Sup) -> None:
    await sup.supervisor.shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        await sup.submit(ECHO)


# -- WORKER-9: stdout is the channel ------------------------------------------------


async def test_WORKER_9_stray_stdout_goes_to_stderr_not_the_channel(
    sup: Sup, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="rifferr.worker")
    run_id = await sup.submit("name: t\nsteps:\n  - {id: a, uses: test.print}\n")
    assert (await sup.finished(run_id)).data["status"] == "succeeded"
    assert "malformed" not in caplog.text
    await sup.supervisor.shutdown()  # flush stderr
    assert "stray output from a plugin" in caplog.text
    assert "raw write to fd 1" in caplog.text


def test_WORKER_9_messages_are_json_lines() -> None:
    from rifferr.worker.protocol import WorkerCommand, encode

    line = encode(WorkerCommand(kind="cancel_run", profile="main", run_id="r1"))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert json.loads(line) == {"kind": "cancel_run", "profile": "main", "run_id": "r1"}


# -- WORKER-10: heartbeats -------------------------------------------------------------


async def test_WORKER_10_heartbeat_continues_while_a_step_blocks(sup: Sup) -> None:
    run_id = await sup.submit(block(3))  # longer than hung_after (2 s)
    assert (await sup.finished(run_id)).data["status"] == "succeeded"


async def test_WORKER_10_silent_worker_is_killed_as_hung(sup: Sup) -> None:
    run_id = await sup.submit(SLOW)
    await sup.wait_for(run_id, "run.step.started")
    pid = sup.supervisor.worker_pid("main")
    assert pid is not None
    os.kill(pid, signal.SIGSTOP)
    finished = await sup.finished(run_id)
    assert finished.data["reason"] == "worker_hung"
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_WORKER_6_real_worker_reports_unusable_profile(sup: Sup) -> None:
    # The real entry point (python -m rifferr.worker); the profile has no beets.beetsdir.
    real = WorkerSupervisor(sup.db, sup.sink, stop_grace=1.0)
    run_id = await real.submit_run("main", ECHO, source_name="wf.yaml")
    finished = await sup.finished(run_id)
    await real.shutdown()
    assert finished.data["reason"] == "worker_failed_to_start"
    assert "has no beets.beetsdir" in str(finished.data["error"])


async def test_WORKER_1_real_worker_runs_beets_import_then_shell(sup: Sup, tmp_path: Path) -> None:
    """The M0 two-step workflow, end to end through a real worker process and beets."""
    from tests.backends.beets.conftest import BeetsEnv

    env = BeetsEnv(tmp_path / "b")
    env.write_config()
    env.inbox_album()
    create_profile(config_root(), "beets", beetsdir=env.beetsdir)
    src = (
        "name: m0\nsteps:\n"
        f"  - {{id: import, uses: beets.import, with: {{path: '{env.inbox}'}}}}\n"
        "  - {id: list, uses: shell, with: {run: [echo, '$albumartist - $album']}}\n"
    )
    real = WorkerSupervisor(sup.db, sup.sink, stop_grace=2.0)
    sup.db.trust(workflow_hash(src), "m0.yaml", "test")
    run_id = await real.submit_run("beets", src, source_name="m0.yaml")
    finished = await sup.wait_for(run_id, timeout=60)
    await real.shutdown()
    assert finished.data["status"] == "succeeded", sup.events(run_id)
    lines = [e.data["line"] for e in sup.events(run_id) if e.type == "run.step.log"]
    assert "Fixture Artist - Fixture Album" in lines


async def test_SECURITY_10_untrusted_workflow_cannot_run_shell_steps(sup: Sup) -> None:
    src = "name: t\nsteps:\n  - {id: s, uses: shell, with: {run: [echo, hi], per: run}}\n"
    run_id = await sup.supervisor.submit_run("main", src, source_name="wf.yaml")
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert job.payload["trusted"] is False
    await sup.finished(run_id)
    [step] = [e for e in sup.events(run_id) if e.type == "run.step.finished"]
    assert "not trusted" in str(step.data["error"])


async def test_SECURITY_10_any_edit_resets_trust(sup: Sup) -> None:
    src = "name: t\nsteps:\n  - {id: s, uses: shell, with: {run: [echo, hi], per: run}}\n"
    sup.db.trust(workflow_hash(src), "wf.yaml", "test")
    run_id = await sup.supervisor.submit_run("main", src + "# edited\n", source_name="wf.yaml")
    job = sup.db.job_for_run(run_id)
    assert job is not None
    assert job.payload["trusted"] is False
    await sup.finished(run_id)


def test_WORKER_8_stopped_worker_process_exits_cleanly(tmp_path: Path) -> None:
    """B-022: with stdin still open (as the supervisor keeps it), a stop request must not
    leave the stdin reader thread aborting interpreter shutdown."""
    import subprocess

    create_profile(config_root(), "main")
    proc = subprocess.Popen(
        [sys.executable, FAKE_WORKER, "--profile", "main", "--db", str(tmp_path / "app.db")],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stderr is not None
    try:
        proc.stdin.write(b'{"kind":"stop","profile":"main"}\n')
        proc.stdin.flush()
        code = proc.wait(timeout=30)  # stdin stays open until the worker has exited
        err = proc.stderr.read()
    finally:
        proc.stdin.close()
        proc.kill()
    assert code == 0, err.decode()[-500:]
    assert b"Fatal Python error" not in err
