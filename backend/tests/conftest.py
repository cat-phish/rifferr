"""Test isolation shared by the whole suite.

- Every test gets HOME, XDG dirs and BEETSDIR under its own tmp_path, so no test
  can read or write the developer's real beets config or library (DEV-TOOLING-6).
- Opening an internet socket fails the test (DEV-TOOLING-7). Unix sockets are
  allowed for local IPC tests. Mock HTTP with respx instead.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

ISOLATED_ENV_DIRS = {
    "HOME": "home",
    "XDG_CONFIG_HOME": "home/.config",
    "XDG_DATA_HOME": "home/.local/share",
    "XDG_CACHE_HOME": "home/.cache",
    "XDG_STATE_HOME": "home/.local/state",
    "XDG_RUNTIME_DIR": "run",
    "BEETSDIR": "beets",
    "RIFFERR_CONFIG_DIR": "rifferr-config",
}

# Variables that could point a test at real user data or real credentials.
SCRUBBED_ENV = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DISCOGS_TOKEN",
    "FANARTTV_API_KEY",
    "LASTFM_API_KEY",
    "RIFFERR_PROFILE",
)

INTERNET_FAMILIES = {socket.AF_INET, socket.AF_INET6}
LOCAL_HOSTS = {None, "", "localhost", "127.0.0.1", "::1"}


class NetworkBlockedError(RuntimeError):
    """Raised when a test tries to open an internet connection."""


def pytest_sessionstart(session: pytest.Session) -> None:
    # Drop anything inherited from the developer's shell before any test runs.
    for name in (*ISOLATED_ENV_DIRS, *SCRUBBED_ENV):
        os.environ.pop(name, None)


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    dirs: dict[str, Path] = {}
    for var, rel in ISOLATED_ENV_DIRS.items():
        path = tmp_path / "env" / rel
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(path))
        dirs[var] = path
    os.chmod(dirs["XDG_RUNTIME_DIR"], 0o700)
    for name in SCRUBBED_ENV:
        monkeypatch.delenv(name, raising=False)
    return dirs


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guard(sock: socket.socket, address: object) -> None:
        if sock.family in INTERNET_FAMILIES:
            raise NetworkBlockedError(
                f"network access is blocked in tests (tried {address!r}); mock it with respx"
            )

    def connect(self: socket.socket, address: object) -> None:
        guard(self, address)
        return real_connect(self, address)  # type: ignore[arg-type]

    def connect_ex(self: socket.socket, address: object) -> int:
        guard(self, address)
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        name = host.decode() if isinstance(host, bytes) else host
        if name not in LOCAL_HOSTS:
            raise NetworkBlockedError(f"DNS lookup of {name!r} is blocked in tests")
        return real_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
