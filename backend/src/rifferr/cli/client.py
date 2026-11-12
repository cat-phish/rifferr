"""Finding and talking to the running server (CLI-2, CLI-7).

The server file says where the server for this config root listens; the bearer
token comes from the 0600 token file (SECURITY-8), never from the command line.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from rifferr.api.security import read_token_file
from rifferr.profile_store import data_root

SERVER_FILE = "server.json"


class NoServerError(Exception):
    """No reachable server for this config root (exit 3)."""


class ServerInfo(BaseModel):
    port: int
    pid: int
    no_auth: bool = False
    started_at: datetime
    uds: str | None = None
    """Unix socket path when the server listens on one instead of TCP."""


def server_file_path(env: Mapping[str, str] | None = None) -> Path:
    return data_root(env) / SERVER_FILE


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def write_server_file(info: ServerInfo, env: Mapping[str, str] | None = None) -> Path:
    path = server_file_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, info.model_dump_json().encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def remove_server_file(pid: int, env: Mapping[str, str] | None = None) -> None:
    """Remove the server file if it is still ours (a newer server may have replaced it)."""
    info = read_server_file(env, check_pid=False)
    if info is not None and info.pid == pid:
        server_file_path(env).unlink(missing_ok=True)


def read_server_file(
    env: Mapping[str, str] | None = None, *, check_pid: bool = True
) -> ServerInfo | None:
    """The live server's info, or None if there is none or the file is stale."""
    try:
        info = ServerInfo.model_validate(json.loads(server_file_path(env).read_text()))
    except (FileNotFoundError, ValueError):
        return None
    if check_pid and not pid_alive(info.pid):
        return None
    return info


def new_server_info(port: int, no_auth: bool, uds: str | None = None) -> ServerInfo:
    return ServerInfo(
        port=port, pid=os.getpid(), no_auth=no_auth, started_at=datetime.now(UTC), uds=uds
    )


class Client:
    """A small synchronous API client. Always sends the app's own Origin: the CLI is
    not a browser, and a ``--no-auth`` server has no token to exempt it (SECURITY-3)."""

    def __init__(self, info: ServerInfo, token: str | None) -> None:
        base = f"http://127.0.0.1:{info.port}"
        headers = {"origin": base}
        if token:
            headers["authorization"] = f"Bearer {token}"
        transport = httpx.HTTPTransport(uds=info.uds) if info.uds else None
        self.info = info
        self.http = httpx.Client(
            base_url=base,
            headers=headers,
            transport=transport,
            timeout=httpx.Timeout(30.0, read=None),
        )

    def close(self) -> None:
        self.http.close()

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.http.get(path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> httpx.Response:
        return self.http.post(path, json={} if body is None else body, **kw)

    def sse(self, path: str, last_event_id: int = 0) -> Iterator[tuple[str, dict[str, Any]]]:
        """``(event type, data)`` for each SSE message until the server closes the stream."""
        headers = {"last-event-id": str(last_event_id)} if last_event_id else {}
        with self.http.stream("GET", path, headers=headers) as r:
            if r.status_code != 200:
                r.read()
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            event, data = "", ""
            for line in r.iter_lines():
                if line == "":
                    if data:
                        yield event, json.loads(data)
                    event, data = "", ""
                elif line.startswith(":"):
                    continue
                elif line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data += line.removeprefix("data: ")


def connect(env: Mapping[str, str] | None = None) -> Client:
    """A client for the running server, or ``NoServerError``."""
    info = read_server_file(env)
    if info is None:
        raise NoServerError("no Rifferr server is running for this config; start `rifferr serve`")
    token = None if info.no_auth else read_token_file(info.port, None if env is None else dict(env))
    if not info.no_auth and token is None:
        raise NoServerError(f"the server on port {info.port} left no token file")
    client = Client(info, token)
    try:
        client.get("/healthz").raise_for_status()
    except httpx.HTTPError as exc:
        client.close()
        raise NoServerError(f"the server on port {info.port} is not reachable: {exc}") from None
    return client
