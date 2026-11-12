"""The ``shell`` step (S-SHELL): run a command once per run, album or item.

Output-only in M0 (``emits: none``). Commands run in their own process group so
a timeout or cancellation can stop everything they started (POSIX only).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator

from rifferr.backends.base import BackendCapabilities, TemplateError
from rifferr.core.profile import env_var_name
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot
from rifferr.engine import StepContext, StepFailure, StepRefused, StepResult

KILL_GRACE = 5.0
"""Seconds between SIGTERM and SIGKILL (SHELL-7)."""
POLL_INTERVAL = 0.05
"""How often a waiting step checks for cancellation."""
READER_GRACE = 1.0
"""How long to wait for output pipes to close after the process exits. A background
child that keeps them open is not waited for."""
MAX_LINE = 64 * 1024
"""Longer lines are split into several log events."""
SECRET_ENV_PREFIX = "RIFFERR_SECRET_"

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ShellParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: list[str] | str = Field(
        description="Command as an argv list, or a string run by /bin/sh -c (needs shell: true)"
    )
    shell: bool = Field(default=False, description="Run a string command through /bin/sh -c")
    per: Literal["run", "album", "item"] = "album"
    timeout: PositiveInt = Field(default=600, description="Seconds before the command is stopped")
    cwd: str | None = Field(default=None, description="Working directory (templated)")
    env: dict[str, str] = Field(default_factory=dict[str, str], description="Extra variables")
    secrets: list[str] = Field(
        default_factory=list[str],
        description="Profile secrets to expose as RIFFERR_SECRET_<NAME>",
    )
    emits: Literal["none"] = "none"

    @field_validator("run")
    @classmethod
    def _non_empty(cls, value: list[str] | str) -> list[str] | str:
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [k for k in value if not _ENV_NAME.match(k)]
        if bad:
            raise ValueError(f"not a valid environment variable name: {', '.join(bad)}")
        return value

    @field_validator("emits", mode="before")
    @classmethod
    def _emits(cls, value: object) -> object:
        if value == "changes":
            raise ValueError("'changes' arrives with ChangeSets in M1; only 'none' is supported")
        return value

    @model_validator(mode="after")
    def _shell_form(self) -> ShellParams:
        if isinstance(self.run, str) and not self.shell:
            raise ValueError(
                "run: a single string needs 'shell: true'; otherwise give an argv list "
                "like [ls, -la, $path]"
            )
        if isinstance(self.run, list) and self.shell:
            raise ValueError("run: 'shell: true' needs a single command string, not a list")
        return self


@dataclass(frozen=True)
class _Target:
    id: str | None
    snapshot: AlbumSnapshot | ItemSnapshot | None
    stdin: bytes
    album_dir: str | None
    item_path: str | None


class ShellStep:
    name = "shell"
    params_model = ShellParams
    requires = BackendCapabilities(query=True, templates=True)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, ShellParams)
        if not ctx.trusted:
            raise StepRefused(
                "this workflow is not trusted, so shell steps can't run; "
                "trust it before running it (F-SECURITY)"
            )
        if sys.platform == "win32":
            raise StepRefused("shell steps need Linux or macOS; on Windows use the Docker image")
        base_env = self._base_env(ctx, params)
        for target in _targets(ctx, params):
            if ctx.cancelled:
                break
            try:
                argv, cwd, extra = _render(ctx, params, target)
            except TemplateError as exc:
                _fail(ctx, target, f"template error: {exc}")
                continue
            env = {**base_env, **extra, **_fixed_env(ctx, target)}
            if cwd is None:
                cwd = target.album_dir or (str(ctx.work_dir) if ctx.work_dir else None)
            problem = await _execute(ctx, argv, cwd, env, target, params.timeout)
            if problem is not None and not ctx.cancelled:
                _fail(ctx, target, problem)
        return StepResult()

    def _base_env(self, ctx: StepContext, params: ShellParams) -> dict[str, str]:
        environ = os.environ if self._environ is None else self._environ
        listed = set(params.secrets)
        hidden = {var for name, var in ctx.secret_env_refs().items() if name not in listed}
        env = {k: v for k, v in environ.items() if k not in hidden}
        if ctx.backend.capabilities.command_env:
            env.update(ctx.backend.command_env())
        for name, value in ctx.vars.items():
            env[env_var_name(name)] = value
        for name in params.secrets:
            env[secret_env_name(name)] = ctx.secret(name).reveal()
        return env


def secret_env_name(name: str) -> str:
    """``discogs_token`` -> ``RIFFERR_SECRET_DISCOGS_TOKEN``."""
    return SECRET_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


# -- targets ------------------------------------------------------------------


def _targets(ctx: StepContext, params: ShellParams) -> list[_Target]:
    if params.per == "run":
        albums = ctx.albums()
        data = {"albums": [a.model_dump(mode="json") for a in albums]}
        return [_Target(None, None, json.dumps(data).encode(), None, None)]
    if params.per == "album":
        return [_Target(a.id, a, a.model_dump_json().encode(), a.path, None) for a in ctx.albums()]
    return [
        _Target(i.id, i, i.model_dump_json().encode(), a.path, i.path)
        for a in ctx.albums("item")
        for i in a.items
    ]


def _render(
    ctx: StepContext, params: ShellParams, target: _Target
) -> tuple[list[str], str | None, dict[str, str]]:
    """Evaluate beets templates per target; never split words (SHELL-2)."""
    snapshot = target.snapshot

    def ev(value: str) -> str:
        return value if snapshot is None else ctx.backend.evaluate(value, snapshot)

    if isinstance(params.run, str):
        # The shell string is not template-evaluated: tag values would become shell
        # code. Scripts read RIFFERR_ALBUM_DIR / RIFFERR_ITEM_PATH or stdin instead.
        argv = ["/bin/sh", "-c", params.run]
    else:
        argv = [ev(a) for a in params.run]
    cwd = ev(params.cwd) if params.cwd is not None else None
    return argv, cwd, {k: ev(v) for k, v in params.env.items()}


def _fixed_env(ctx: StepContext, target: _Target) -> dict[str, str]:
    env = {"RIFFERR_RUN_ID": ctx.run_id, "RIFFERR_PROFILE": ctx.profile}
    if target.album_dir is not None:
        env["RIFFERR_ALBUM_DIR"] = target.album_dir
    if target.item_path is not None:
        env["RIFFERR_ITEM_PATH"] = target.item_path
    return env


def _fail(ctx: StepContext, target: _Target, message: str) -> None:
    if target.id is None:
        raise StepFailure(message)
    ctx.fail_target(target.id, message)


# -- processes ----------------------------------------------------------------


async def _execute(
    ctx: StepContext,
    argv: list[str],
    cwd: str | None,
    env: dict[str, str],
    target: _Target,
    timeout: int,
) -> str | None:
    """Run one command; return a failure message, or None on success."""
    if cwd is not None and not Path(cwd).is_dir():
        return f"working directory {cwd} does not exist"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        return f"could not start {argv[0]}: {exc.strerror or exc}"
    io: list[asyncio.Task[Any]] = []
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        io.append(asyncio.ensure_future(_feed(proc.stdin, target.stdin)))
        io.append(asyncio.ensure_future(_pump(ctx, proc.stdout, "stdout", target.id)))
        io.append(asyncio.ensure_future(_pump(ctx, proc.stderr, "stderr", target.id)))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stopped: str | None = None
        while proc.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), POLL_INTERVAL)
            if proc.returncode is not None:
                break
            if ctx.cancelled:
                stopped = "cancelled"
            elif loop.time() >= deadline:
                stopped = f"timed out after {timeout} s"
            if stopped is not None:
                await _terminate(proc)
                break
        await asyncio.wait(io, timeout=READER_GRACE)
        if stopped is not None:
            return stopped
        code = proc.returncode
        if code is None or code == 0:
            return None
        if code < 0:
            return f"killed by signal {signal.Signals(-code).name}"
        return f"exited with status {code}"
    finally:
        for task in io:
            task.cancel()
        if proc.returncode is None:  # the step itself was cancelled mid-wait
            _signal_group(proc.pid, signal.SIGKILL)


async def _feed(stdin: asyncio.StreamWriter, data: bytes) -> None:
    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
        stdin.write(data)
        await stdin.drain()
        stdin.close()
        await stdin.wait_closed()


async def _pump(
    ctx: StepContext, stream: asyncio.StreamReader, name: str, target: str | None
) -> None:
    buffer = b""
    while True:
        chunk = await stream.read(MAX_LINE)
        if not chunk:
            break
        buffer += chunk
        *lines, buffer = buffer.split(b"\n")
        while len(buffer) >= MAX_LINE:
            lines.append(buffer[:MAX_LINE])
            buffer = buffer[MAX_LINE:]
        for line in lines:
            ctx.log(_decode(line), stream=name, target=target)
    if buffer:
        ctx.log(_decode(buffer), stream=name, target=target)


def _decode(line: bytes) -> str:
    return line.decode(errors="replace").removesuffix("\r")


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the process group, then SIGKILL after KILL_GRACE (SHELL-7, SHELL-9)."""
    _signal_group(proc.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()), KILL_GRACE)
    # Kill the group even if the leader exited: its children may still be running.
    _signal_group(proc.pid, signal.SIGKILL)
    await proc.wait()


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)
