"""``rifferr serve`` (CLI-1..6)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import webbrowser
from pathlib import Path
from types import FrameType

import uvicorn

from rifferr.api.app import DEFAULT_PORT, REPO_FRONTEND, VITE_PORT, ApiSettings, create_app
from rifferr.api.security import (
    EXCHANGE_PATH,
    SIGN_IN_LINK_PATH,
    BindRefusedError,
    check_bind,
    normalize_host,
    token_file_path,
)
from rifferr.cli.client import (
    NoServerError,
    connect,
    new_server_info,
    remove_server_file,
    write_server_file,
)

log = logging.getLogger("rifferr.cli")

VITE_STOP_GRACE = 5.0
_TOKEN_IN_QUERY = re.compile(r"([?&]token=)[^&\s]*")


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("serve", help="start the web app and API server")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port (default: {DEFAULT_PORT})")
    p.add_argument(
        "--no-auth", action="store_true", help="no local token (Host/Origin checks stay on)"
    )
    p.add_argument("--no-browser", action="store_true", help="don't open a browser")
    p.add_argument("--dev", action="store_true", help="also start the Vite dev server")
    p.add_argument(
        "--vite-port", type=int, default=VITE_PORT, help=f"Vite port (default: {VITE_PORT})"
    )
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        type=_host_arg,
        metavar="NAME[:PORT]",
        help="also accept this Host, e.g. a reverse proxy's name; repeatable. The proxy "
        "must pass Host and Origin through unchanged and should serve https",
    )
    p.add_argument("--uds", type=Path, default=None, help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_serve)


def add_sign_in_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("sign-in", help="print a one-time sign-in link for the running server")
    p.set_defaults(func=cmd_sign_in)


def _host_arg(value: str) -> str:
    try:
        return normalize_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


class RedactTokens(logging.Filter):
    """Keeps the token out of uvicorn's access log (CLI-4)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _TOKEN_IN_QUERY.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = _TOKEN_IN_QUERY.sub(r"\1[redacted]", record.msg)
        return True


def exchange_url(port: int, token: str) -> str:
    return f"http://127.0.0.1:{port}{EXCHANGE_PATH}?token={token}"


def startup_lines(
    settings: ApiSettings, *, tty: bool, ui_port: int, link: str | None = None
) -> list[str]:
    """What ``serve`` prints. A sign-in link appears only on a terminal (CLI-3), and it
    carries a one-time code, never the local token (SEC-43)."""
    plain = f"http://127.0.0.1:{ui_port}/"
    if settings.no_auth:
        return [f"Rifferr is running at {plain} (no auth)"]
    if tty:
        link = link or exchange_url(ui_port, settings.sessions.sign_in_code())
        return [f"Rifferr is running. Open (works once): {link}"]
    return [
        f"Rifferr is running at {plain}",
        "To sign in, run `rifferr sign-in` for a one-time link "
        f"(it uses the token in {token_file_path(settings.port)})",
    ]


def _bind(args: argparse.Namespace) -> socket.socket:
    if args.uds is not None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        args.uds.unlink(missing_ok=True)
        old = os.umask(0o077)  # only this user may connect
        try:
            sock.bind(str(args.uds))
        finally:
            os.umask(old)
        return sock
    family = socket.AF_INET6 if ":" in args.host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host.strip("[]"), args.port))
    return sock


def _start_vite(port: int, vite_port: int) -> subprocess.Popen[bytes]:
    frontend = REPO_FRONTEND.parent
    pnpm = shutil.which("pnpm")
    if not (frontend / "package.json").is_file() or pnpm is None:
        raise SystemExit(_fail(2, "--dev needs the repo's frontend/ folder and pnpm on PATH"))
    env = {**os.environ, "RIFFERR_BACKEND": f"http://127.0.0.1:{port}"}
    return subprocess.Popen(
        [pnpm, "dev", "--port", str(vite_port), "--strictPort"],
        cwd=frontend,
        env=env,
        start_new_session=True,  # its own group, so stopping it reaches node too
    )


def _stop_vite(proc: subprocess.Popen[bytes]) -> None:
    for sig, wait in ((signal.SIGTERM, VITE_STOP_GRACE), (signal.SIGKILL, None)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _fail(code: int, message: str) -> int:
    print(f"rifferr serve: {message}", file=sys.stderr)
    return code


def _graceful(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt  # uvicorn re-raises the signal it caught after shutting down


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        check_bind(args.host)
    except BindRefusedError as exc:
        return _fail(2, str(exc))
    if args.no_auth and args.allowed_host:  # SEC-46: a proxied server keeps its auth
        return _fail(2, "--allowed-host can't be used with --no-auth")
    settings = ApiSettings(
        port=args.port,
        host=args.host,
        no_auth=args.no_auth,
        dev=args.dev,
        vite_port=args.vite_port,
        extra_hosts=tuple(args.allowed_host),
    )
    try:
        sock = _bind(args)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return _fail(1, f"port {args.port} on {args.host} is already in use")
        return _fail(1, f"can't listen on {args.host}:{args.port}: {exc.strerror}")
    config = uvicorn.Config(create_app(settings), lifespan="on", log_level="info")
    logging.getLogger("uvicorn.access").addFilter(RedactTokens())
    server = uvicorn.Server(config)
    vite = _start_vite(args.port, args.vite_port) if args.dev else None
    info = new_server_info(args.port, args.no_auth, str(args.uds) if args.uds else None)
    ui_port = args.vite_port if args.dev else args.port
    previous = signal.signal(signal.SIGTERM, _graceful)

    async def started() -> None:
        while not server.started and not server.should_exit:
            await asyncio.sleep(0.05)
        if not server.started:
            return
        write_server_file(info)
        # One code for the printed link and the browser: once the browser has used it,
        # the link in the scrollback is dead (SEC-43).
        link = None if args.no_auth else exchange_url(ui_port, settings.sessions.sign_in_code())
        for line in startup_lines(settings, tty=sys.stdout.isatty(), ui_port=ui_port, link=link):
            print(line, flush=True)
        if not args.no_browser and link is not None:
            webbrowser.open(link)
        elif not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{ui_port}/")

    vite_failed: list[int] = []

    async def watch_vite() -> None:
        """A dead Vite means the printed URL is wrong (or someone else's): stop (CLI-5)."""
        assert vite is not None
        while (code := vite.poll()) is None:
            await asyncio.sleep(0.2)
        if not server.should_exit:
            vite_failed.append(code)
            print(
                f"rifferr serve: the Vite dev server exited (status {code}); stopping",
                file=sys.stderr,
            )
            server.should_exit = True

    async def main() -> None:
        tasks = [asyncio.create_task(started())]
        if vite is not None:
            tasks.append(asyncio.create_task(watch_vite()))
        try:
            await server.serve(sockets=[sock])
        finally:
            for task in tasks:
                task.cancel()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
        remove_server_file(info.pid)
        if vite is not None:
            _stop_vite(vite)
        sock.close()
        if args.uds is not None:
            args.uds.unlink(missing_ok=True)
    return 0 if server.started and not vite_failed else 1


def cmd_sign_in(args: argparse.Namespace) -> int:
    """``rifferr sign-in``: ask the running server for a one-time sign-in link (SEC-43)."""
    try:
        client = connect()
    except NoServerError as exc:
        print(f"rifferr sign-in: {exc}", file=sys.stderr)
        return 3
    try:
        if client.info.no_auth:
            print(f"http://127.0.0.1:{client.info.port}/ (no auth)")
            return 0
        r = client.post(SIGN_IN_LINK_PATH)
        if r.status_code != 200:
            print(f"rifferr sign-in: the server answered {r.status_code}", file=sys.stderr)
            return 1
        body = r.json()
        minutes = int(body["expires_in"]) // 60
        print(f"http://127.0.0.1:{client.info.port}{body['path']}")
        print(f"(works once, for {minutes} minutes)", file=sys.stderr)
        return 0
    finally:
        client.close()
