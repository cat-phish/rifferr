from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rifferr.api.app import ApiSettings
from rifferr.api.security import token_file_path
from rifferr.appdb import AppDB
from rifferr.cli.client import (
    ServerInfo,
    connect,
    read_server_file,
    server_file_path,
    write_server_file,
)
from rifferr.cli.run import format_event, parse_vars
from rifferr.cli.serve import RedactTokens, exchange_url, startup_lines
from rifferr.profile_store import app_db_path, config_root, profile_dir
from rifferr.trust import workflow_hash
from tests.cli.conftest import (
    ECHO,
    FAIL,
    SLOW,
    STUBBORN,
    Server,
    add_workflow,
    cli,
    make_profile,
    start_server,
    wf,
)


def run_cli(*argv: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "rifferr.cli", *argv],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def runs() -> list[str]:
    db = AppDB(app_db_path())
    try:
        return [j.state for j in db.list_runs()]
    finally:
        db.close()


# -- CLI-1: serve ----------------------------------------------------------------------------------


def test_CLI_1_non_loopback_host_refuses_to_start(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli("serve", "--host", "0.0.0.0", "--no-browser") == 2
    assert "requires authentication" in capsys.readouterr().err
    assert read_server_file() is None


def test_CLI_1_port_in_use_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert cli("serve", "--port", str(port), "--no-browser") == 1
    assert f"port {port}" in capsys.readouterr().err


def test_CLI_1_serve_answers_on_the_api(server: Server) -> None:
    client = connect()
    try:
        assert client.get("/api/profiles").json()[0]["name"] == "main"
    finally:
        client.close()


# -- CLI-2: server file ----------------------------------------------------------------------------


def test_CLI_2_server_file_written_and_removed(server: Server) -> None:
    path = server_file_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["pid"] == server.proc.pid
    assert data["port"] == 8765
    assert data["no_auth"] is False
    token = token_file_path(8765).read_text()
    assert token not in path.read_text()
    assert server.stop() == 0
    assert not path.exists()


def test_CLI_2_stale_server_file_is_ignored_and_replaced(tmp_path: Path) -> None:
    dead = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True
    )
    stale = ServerInfo(port=8765, pid=int(dead.stdout), started_at=datetime.now(UTC))
    write_server_file(stale)
    assert read_server_file() is None
    make_profile(tmp_path)
    s = start_server(tmp_path, "--no-browser")  # waits until the file names the new pid
    assert s.stop() == 0


# -- CLI-3 / CLI-4: token handling -----------------------------------------------------------------


def test_CLI_3_token_printed_only_on_a_terminal() -> None:
    s = ApiSettings(port=8765, token="T0KEN")
    assert any("T0KEN" in line for line in startup_lines(s, tty=True, ui_port=8765))
    lines = startup_lines(s, tty=False, ui_port=8765)
    assert not any("T0KEN" in line for line in lines)
    assert any(str(token_file_path(8765)) in line for line in lines)
    assert exchange_url(5173, "T0KEN") == "http://127.0.0.1:5173/auth/exchange?token=T0KEN"


def test_CLI_3_serve_output_has_no_token_and_browser_gets_it(tmp_path: Path) -> None:
    make_profile(tmp_path)
    seen = tmp_path / "browser-url"
    browser = tmp_path / "browser.sh"
    browser.write_text(f'#!/bin/sh\nprintf %s "$1" > {seen}\n')
    browser.chmod(0o755)
    s = start_server(tmp_path, env={"BROWSER": str(browser)})
    deadline = time.monotonic() + 10
    while not seen.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    token = token_file_path(8765).read_text()
    assert s.stop() == 0
    assert seen.read_text() == exchange_url(8765, token)
    assert token not in s.stdout.decode() + s.stderr.decode()
    assert "Rifferr is running at http://127.0.0.1:8765/" in s.stdout.decode()


def test_CLI_4_access_log_redacts_the_token(server: Server) -> None:
    client = connect()
    try:
        token = token_file_path(8765).read_text()
        r = client.http.get(f"/auth/exchange?token={token}", follow_redirects=False)
        assert r.status_code == 303
        client.http.get("/auth/exchange?token=not-the-token")
    finally:
        client.close()
    server.stop()
    logs = server.stdout.decode() + server.stderr.decode()
    assert "/auth/exchange?token=[redacted]" in logs
    assert token not in logs
    assert "not-the-token" not in logs


def test_CLI_4_filter_redacts_args_and_message() -> None:
    import logging

    record = logging.LogRecord(
        "uvicorn.access", 20, "", 0, '%s "%s"', ("a", "/x?a=1&token=abc"), None
    )
    RedactTokens().filter(record)
    assert record.getMessage() == 'a "/x?a=1&token=[redacted]"'


# -- CLI-5: --dev ----------------------------------------------------------------------------------


def test_CLI_5_dev_starts_and_stops_vite(tmp_path: Path) -> None:
    make_profile(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "pnpm-call"
    pnpm = bin_dir / "pnpm"
    pnpm.write_text(f'#!/bin/sh\necho "$@ $RIFFERR_BACKEND $PWD $$" > {record}\nexec sleep 300\n')
    pnpm.chmod(0o755)
    path = f"{bin_dir}:{os.environ['PATH']}"
    s = start_server(tmp_path, "--dev", "--no-browser", "--vite-port", "5199", env={"PATH": path})
    deadline = time.monotonic() + 10
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    args, backend, cwd, pid = record.read_text().rsplit(maxsplit=3)
    assert args == "dev --port 5199 --strictPort"
    assert backend == "http://127.0.0.1:8765"
    assert cwd.endswith("frontend")
    assert s.stop() == 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid), 0)
    assert "http://127.0.0.1:5199/" in s.stdout.decode()


def test_CLI_5_serve_stops_when_vite_dies(tmp_path: Path) -> None:
    make_profile(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pnpm = bin_dir / "pnpm"
    pnpm.write_text("#!/bin/sh\nsleep 1\necho 'Port 5173 is already in use' >&2\nexit 1\n")
    pnpm.chmod(0o755)
    s = start_server(
        tmp_path, "--dev", "--no-browser", env={"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    )
    s.stdout, s.stderr = s.proc.communicate(timeout=30)
    assert s.proc.returncode == 1
    assert "Vite dev server exited (status 1)" in s.stderr.decode()
    assert not server_file_path().exists()


def test_CLI_5_dev_without_pnpm_exits_2(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "rifferr.cli",
            "serve",
            "--dev",
            "--no-browser",
            "--uds",
            str(tmp_path / "s.sock"),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(empty)},
        timeout=30,
    )
    assert proc.returncode == 2
    assert "pnpm" in proc.stderr
    assert read_server_file() is None


# -- CLI-6: shutdown -------------------------------------------------------------------------------


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_CLI_6_signal_shuts_down_cleanly(tmp_path: Path, sig: signal.Signals) -> None:
    make_profile(tmp_path)
    add_workflow("slow", SLOW, trusted=True)
    s = start_server(tmp_path, "--no-browser")
    assert run_cli("run", "slow", "--profile", "main", "--no-follow").returncode == 0
    deadline = time.monotonic() + 15
    while runs() != ["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert s.stop(sig) == 0, s.stderr.decode()[-2000:]
    assert not server_file_path().exists()
    assert not token_file_path(8765).exists()
    assert not s.uds.exists()
    assert runs()[0] in ("cancelled", "failed")


# -- CLI-7 / CLI-8 / CLI-9: run --------------------------------------------------------------------


def test_CLI_7_run_without_server_exits_3(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli("run", "echo", "--profile", "main") == 3
    assert "rifferr serve" in capsys.readouterr().err


def test_CLI_7_run_unknown_workflow_exits_2(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli("run", "nope", "--profile", "main", "--follow") == 2
    assert "not found" in capsys.readouterr().err


def test_CLI_8_follow_prints_readable_lines_and_exits_0(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    add_workflow("echo", ECHO, trusted=True)
    assert cli("run", "echo", "--profile", "main", "--follow") == 0
    out = capsys.readouterr().out.splitlines()
    assert "started (echo @ main)" in out[0]
    assert any(line.endswith("[hello] stdout: hello world") for line in out)
    assert out[-1].endswith("succeeded")


def test_CLI_8_json_prints_one_event_per_line(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    add_workflow("echo", ECHO, trusted=True)
    assert cli("run", "echo", "--profile", "main", "--json") == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "run.started"
    assert events[-1]["type"] == "run.finished"
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))


def test_CLI_8_no_follow_prints_the_run_id(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    add_workflow("echo", ECHO, trusted=True)
    assert cli("run", "echo", "--profile", "main", "--no-follow", "--var", "x=1") == 0
    run_id = capsys.readouterr().out.strip()
    assert len(run_id) == 26


def test_CLI_8_format_event() -> None:
    base = {"ts": "2026-11-11T12:00:00+00:00", "run_id": "R", "seq": 1}
    started = format_event({**base, "type": "run.started", "data": {}}, "wf", "main")
    assert started is not None
    assert started.endswith("run R started (wf @ main)")
    log = format_event(
        {**base, "type": "run.step.log", "step_id": "s", "data": {"stream": "stderr", "line": "x"}},
        "wf",
        "main",
    )
    assert log is not None
    assert log.endswith("[s] stderr: x")
    ok = {**base, "type": "run.step.finished", "step_id": "s", "data": {"status": "succeeded"}}
    assert format_event(ok, "wf", "main") is None


def test_CLI_9_failed_run_exits_1(server: Server, capsys: pytest.CaptureFixture[str]) -> None:
    add_workflow("fail", FAIL, trusted=True)
    assert cli("run", "fail", "--profile", "main", "--follow") == 1
    out = capsys.readouterr().out
    assert "stderr: bad" in out
    assert out.rstrip().endswith("failed")


def test_CLI_9_validation_error_exits_2_with_file_line_col(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    path = add_workflow("bad", "name: x\nsteps:\n  - {id: a, uses: nope}\n")
    assert cli("run", "bad", "--profile", "main", "--follow") == 2
    err = capsys.readouterr().err
    assert f"{path}:3:" in err
    assert runs() == []


def test_CLI_9_bad_var_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_vars(["a=b=c", "e="]) == {"a": "b=c", "e": ""}
    assert cli("run", "echo", "--profile", "main", "--var", "novalue") == 2


def _follow_in_subprocess(name: str) -> subprocess.Popen[str]:
    proc = subprocess.Popen(
        [sys.executable, "-m", "rifferr.cli", "run", name, "--profile", "main", "--follow"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:  # wait until the step runs
        if "started (" in line:
            break
    deadline = time.monotonic() + 15
    while runs()[:1] != ["running"] and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    return proc


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_CLI_9_signal_cancels_the_run(server: Server, sig: signal.Signals, code: int) -> None:
    add_workflow("slow", SLOW, trusted=True)
    proc = _follow_in_subprocess("slow")
    proc.send_signal(sig)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == code, err
    assert "cancelling" in err
    assert runs() == ["cancelled"]


def test_CLI_9_second_signal_stops_waiting(server: Server) -> None:
    add_workflow("stubborn", STUBBORN, trusted=True)
    proc = _follow_in_subprocess("stubborn")
    proc.send_signal(signal.SIGINT)
    time.sleep(0.5)
    started = time.monotonic()
    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 130
    assert time.monotonic() - started < 3  # not the 5 s kill grace
    assert "stopped waiting" in err


# -- CLI-10: trust hint ----------------------------------------------------------------------------


def test_CLI_10_untrusted_shell_run_prints_trust_hint(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    add_workflow("echo", ECHO)
    assert cli("run", "echo", "--profile", "main", "--follow") == 1
    assert "rifferr trust" in capsys.readouterr().err


# -- CLI-11: validate ------------------------------------------------------------------------------


def test_CLI_11_validate_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = tmp_path / "good.yaml"
    good.write_text(ECHO)
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nsteps:\n  - {id: a, uses: nope}\n")
    assert cli("validate", str(good)) == 0
    captured = capsys.readouterr()
    assert f"{good}: ok" in captured.out
    assert "--profile" in captured.err  # says the profile checks were skipped
    assert cli("validate", str(good), str(bad), str(tmp_path / "missing.yaml")) == 1
    out = capsys.readouterr().out
    assert f"{bad}:3:" in out
    assert f"{tmp_path / 'missing.yaml'}: " in out


def test_CLI_11_safety_report_does_not_change_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "net.yaml"
    f.write_text(wf("{id: get, uses: shell, with: {per: run, run: [curl, 'https://example.org']}}"))
    assert cli("validate", "--safety", str(f)) == 0
    assert f"{f}: safety: [get] network_tool" in capsys.readouterr().out


def test_CLI_11_profile_checks_only_with_profile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_profile(tmp_path)
    f = tmp_path / "vars.yaml"
    f.write_text(wf("{id: a, uses: shell, with: {per: run, run: [ls, '{{ vars.inbox }}']}}"))
    assert cli("validate", str(f)) == 0
    assert cli("validate", "--profile", "main", str(f)) == 1
    assert "inbox" in capsys.readouterr().out
    assert cli("validate", "--profile", "nobody", str(f)) == 2


# -- CLI-12: trust ---------------------------------------------------------------------------------


def _trusted(text: str) -> bool:
    db = AppDB(app_db_path())
    try:
        return db.is_trusted(workflow_hash(text))
    finally:
        db.close()


def test_CLI_12_trust_yes_records_the_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "echo.yaml"
    f.write_text(ECHO)
    assert cli("trust", str(f), "--yes") == 0
    out = capsys.readouterr().out
    assert workflow_hash(ECHO) in out
    assert "safety:" in out
    assert _trusted(ECHO)


def test_CLI_12_no_terminal_no_yes_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "echo.yaml"
    f.write_text(ECHO)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli("trust", str(f)) == 2
    assert "--yes" in capsys.readouterr().err
    assert not _trusted(ECHO)


@pytest.mark.parametrize(("answer", "code"), [("y", 0), ("", 1)])
def test_CLI_12_prompt_on_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer: str, code: int
) -> None:
    f = tmp_path / "echo.yaml"
    f.write_text(ECHO)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    assert cli("trust", str(f)) == code
    assert _trusted(ECHO) is (code == 0)


def test_CLI_12_invalid_file_is_not_trusted(tmp_path: Path) -> None:
    make_profile(tmp_path)
    f = tmp_path / "bad.yaml"
    f.write_text("steps: [\n")
    assert cli("trust", str(f), "--yes") == 1
    v = tmp_path / "vars.yaml"
    text = wf("{id: a, uses: shell, with: {per: run, run: [ls, '{{ vars.inbox }}']}}")
    v.write_text(text)
    assert cli("trust", str(v), "--yes", "--profile", "main") == 1
    assert not _trusted(text)


# -- CLI-13: profiles ------------------------------------------------------------------------------


def test_CLI_13_profile_create_and_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli("profile", "create", "main") == 0
    assert "beets.beetsdir" in capsys.readouterr().out
    assert cli("profile", "create", "main") == 1
    beets = tmp_path / "b"
    beets.mkdir()
    assert cli("profile", "create", "other", "--beetsdir", str(beets)) == 0
    (profile_dir(config_root(), "other") / "profile.yaml").write_text("backend: [\n")
    capsys.readouterr()
    assert cli("profile", "list") == 0
    out = capsys.readouterr().out
    assert "main\tbeets" in out
    assert "other\terror:" in out


# -- CLI-14: no secrets in output ------------------------------------------------------------------


def test_CLI_14_no_command_prints_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    value, ref = "s3cr3t-value-xyz", "RIFFERR_TEST_LASTFM_KEY"
    monkeypatch.setenv(ref, value)
    root = make_profile(tmp_path)
    p = root / "profile.yaml"
    p.write_text(p.read_text().replace("secrets: {}", f"secrets: {{ lastfm: env:{ref} }}"))
    text = wf(
        "{id: leak, uses: shell, with: {per: run, run: [sh, -c, 'echo $RIFFERR_SECRET_LASTFM'],"
        " secrets: [lastfm]}}"
    )
    path = add_workflow("leak", text)
    s = start_server(tmp_path, "--no-browser")
    try:
        assert cli("-v", "trust", str(path), "--yes", "--profile", "main") == 0
        assert cli("-v", "validate", "--safety", "--profile", "main", str(path)) == 0
        assert cli("-v", "profile", "list") == 0
        assert cli("-v", "run", "leak", "--profile", "main", "--follow") == 0
        assert cli("-v", "run", "leak", "--profile", "main", "--json") == 0
    finally:
        s.stop()
    captured = capfd.readouterr()
    everything = captured.out + captured.err + s.stdout.decode() + s.stderr.decode()
    assert value not in everything
    assert ref not in everything


# -- CLI-15: help and version ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["serve"],
        ["run"],
        ["validate"],
        ["trust"],
        ["profile"],
        ["profile", "create"],
        ["profile", "list"],
    ],
)
def test_CLI_15_every_command_has_help(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli(*argv, "--help")
    assert exc.value.code == 0
    assert "usage: rifferr" in capsys.readouterr().out


def test_CLI_15_version_without_importing_beets() -> None:
    proc = run_cli("--version")
    assert proc.returncode == 0
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("rifferr ")
    assert lines[1].startswith("beets 2.")
    check = subprocess.run(
        [sys.executable, "-c", "import sys, rifferr.cli.main; print('beets' in sys.modules)"],
        capture_output=True,
        text=True,
    )
    assert check.stdout.strip() == "False"
