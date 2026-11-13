"""Running a subprocess for a step (S-SHELL, S-BEETS-COMMAND).

The command runs in its own process group, so a timeout or cancellation stops
everything it started (POSIX only). stdout and stderr stream into the run log
line by line (SHELL-6), unless stdout is handed to a sink instead (SHELL-14).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rifferr.engine import StepContext

KILL_GRACE = 5.0
"""Seconds between SIGTERM and SIGKILL (SHELL-7)."""
POLL_INTERVAL = 0.05
"""How often a waiting step checks for cancellation."""
READER_GRACE = 1.0
"""How long to wait for output pipes to close after the process exits. A background
child that keeps them open is not waited for."""
MAX_LINE = 64 * 1024
"""Longer lines are split into several log events."""


async def run_process(
    ctx: StepContext,
    argv: list[str],
    *,
    cwd: str | None,
    env: dict[str, str],
    timeout: float,
    timeout_text: str | None = None,
    stdin: bytes | None = None,
    target: str | None = None,
    stdout_sink: Callable[[bytes], None] | None = None,
) -> str | None:
    """Run one command; return a failure message, or None on success.

    ``stdin=None`` gives the process an empty stdin (``/dev/null``). With
    ``stdout_sink``, stdout chunks go there instead of the log. ``timeout_text`` is
    how the timeout is named in the failure message (default ``"<timeout> s"``).
    """
    if cwd is not None and not Path(cwd).is_dir():
        return f"working directory {cwd} does not exist"
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
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
        assert proc.stdout is not None
        assert proc.stderr is not None
        if stdin is not None:
            assert proc.stdin is not None
            io.append(asyncio.ensure_future(_feed(proc.stdin, stdin)))
        if stdout_sink is None:
            io.append(asyncio.ensure_future(_pump(ctx, proc.stdout, "stdout", target)))
        else:
            io.append(asyncio.ensure_future(_gather(proc.stdout, stdout_sink)))
        io.append(asyncio.ensure_future(_pump(ctx, proc.stderr, "stderr", target)))
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
                stopped = f"timed out after {timeout_text or f'{timeout:g} s'}"
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


async def _gather(stream: asyncio.StreamReader, sink: Callable[[bytes], None]) -> None:
    while chunk := await stream.read(MAX_LINE):
        sink(chunk)  # keeps draining, so the child never blocks on a full pipe


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
