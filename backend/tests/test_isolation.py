from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from tests.conftest import NetworkBlockedError


@pytest.mark.parametrize("var", ["HOME", "XDG_CONFIG_HOME", "BEETSDIR", "RIFFERR_CONFIG_DIR"])
def test_DEV_TOOLING_6_env_dirs_point_into_tmp_path(tmp_path: Path, var: str) -> None:
    assert Path(os.environ[var]).is_relative_to(tmp_path)


def test_DEV_TOOLING_6_real_beets_config_is_unreachable(tmp_path: Path) -> None:
    assert Path("~/.config/beets").expanduser().is_relative_to(tmp_path)


def test_DEV_TOOLING_6_credentials_are_scrubbed() -> None:
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "DISCOGS_TOKEN" not in os.environ


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_DEV_TOOLING_7_internet_sockets_are_blocked(family: socket.AddressFamily) -> None:
    addr = ("127.0.0.1", 9) if family == socket.AF_INET else ("::1", 9)
    with socket.socket(family, socket.SOCK_STREAM) as s, pytest.raises(NetworkBlockedError):
        s.connect(addr)


def test_DEV_TOOLING_7_create_connection_is_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.create_connection(("127.0.0.1", 9), timeout=1)


def test_DEV_TOOLING_7_dns_lookups_are_blocked() -> None:
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("example.com", 443)


def test_DEV_TOOLING_7_unix_sockets_are_allowed(tmp_path: Path) -> None:
    path = str(tmp_path / "s.sock")
    with socket.socket(socket.AF_UNIX) as server, socket.socket(socket.AF_UNIX) as client:
        server.bind(path)
        server.listen(1)
        client.connect(path)
