"""``rifferr serve`` as a real subprocess on a Unix socket (the test network guard blocks
loopback TCP), with a real worker and a temp beets profile under tmp_path."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from rifferr.appdb import AppDB
from rifferr.cli import main
from rifferr.cli.client import ServerInfo, read_server_file
from rifferr.profile_store import (
    WORKFLOWS_DIR,
    app_db_path,
    config_root,
    create_profile,
    profile_dir,
)
from rifferr.trust import workflow_hash

BEETS_CONFIG = """\
directory: {root}/library
library: {root}/library.db
plugins: []
threaded: no
import:
  autotag: no
"""


def wf(*steps: str, extra: str = "") -> str:
    return "name: t\n" + extra + "steps:\n" + "".join(f"  - {s}\n" for s in steps)


ECHO = wf("{id: hello, uses: shell, with: {per: run, run: [echo, hello world]}}")
FAIL = wf("{id: boom, uses: shell, with: {per: run, run: [sh, -c, 'echo bad >&2; exit 3']}}")
SLOW = wf("{id: wait, uses: shell, with: {per: run, run: [sleep, '30']}}")
STUBBORN = wf(
    "{id: wait, uses: shell, with: {per: run, run: [sh, -c, \"trap '' TERM; sleep 30\"]}}"
)


def make_profile(tmp_path: Path, name: str = "main") -> Path:
    beets = tmp_path / f"beets-{name}"
    beets.mkdir()
    (tmp_path / f"lib-{name}" / "library").mkdir(parents=True)
    (beets / "config.yaml").write_text(BEETS_CONFIG.format(root=tmp_path / f"lib-{name}"))
    return create_profile(config_root(), name, beets)


def add_workflow(name: str, text: str, *, trusted: bool = False, profile: str = "main") -> Path:
    path = profile_dir(config_root(), profile) / WORKFLOWS_DIR / f"{name}.yaml"
    path.write_text(text)
    if trusted:
        db = AppDB(app_db_path())
        try:
            db.trust(workflow_hash(text), str(path), "test")
        finally:
            db.close()
    return path


@dataclass
class Server:
    proc: subprocess.Popen[bytes]
    uds: Path
    info: ServerInfo
    stdout: bytes = b""
    stderr: bytes = b""
    code: int | None = None
    extra: dict[str, str] = field(default_factory=dict[str, str])

    def stop(self, sig: int = signal.SIGTERM, timeout: float = 30) -> int:
        if self.code is None:
            self.proc.send_signal(sig)
            self.stdout, self.stderr = self.proc.communicate(timeout=timeout)
            self.code = self.proc.returncode
        assert self.code is not None
        return self.code


def start_server(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> Server:
    uds = tmp_path / "rifferr.sock"
    proc = subprocess.Popen(
        [sys.executable, "-m", "rifferr.cli", "serve", "--uds", str(uds), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        info = read_server_file()
        if info is not None and info.pid == proc.pid:
            return Server(proc, uds, info)
        if proc.poll() is not None:
            _out, err = proc.communicate()
            raise AssertionError(f"serve exited {proc.returncode}: {err.decode()[-2000:]}")
        time.sleep(0.05)
    proc.kill()
    raise AssertionError("serve did not start")


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    make_profile(tmp_path)
    s = start_server(tmp_path, "--no-browser")
    yield s
    s.stop()


def cli(*argv: str) -> int:
    return main(list(argv))
