"""``rifferr run`` (CLI-7..10): submit through the server, follow, map exit codes."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Any

import httpx

from rifferr.cli.client import Client, NoServerError, connect

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NO_SERVER = 0, 1, 2, 3
EXIT_SIGNAL = {signal.SIGINT: 130, signal.SIGTERM: 143}


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("run", help="start a workflow run on the running server")
    p.add_argument(
        "workflow", help="workflow file name (without .yaml) in the profile's workflows/"
    )
    p.add_argument("--profile", required=True)
    p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="run variable")
    p.add_argument(
        "--input",
        type=Path,
        metavar="PATH",
        help="folder for {{ run.input }} (default: the workflow's input)",
    )
    follow = p.add_mutually_exclusive_group()
    follow.add_argument("--follow", dest="follow", action="store_true", default=None)
    follow.add_argument("--no-follow", dest="follow", action="store_false")
    p.add_argument("--json", action="store_true", help="print one event JSON per line")
    p.set_defaults(func=cmd_run)


class _Cancel(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


class _Abort(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


def parse_vars(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"--var needs KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def _err(message: str) -> None:
    print(f"rifferr run: {message}", file=sys.stderr)


def _message(r: httpx.Response) -> str:
    try:
        return str(r.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return f"HTTP {r.status_code}"


def _print_validation(client: Client, args: argparse.Namespace, r: httpx.Response) -> None:
    where = args.workflow
    detail = client.get(f"/api/workflows/{args.workflow}", params={"profile": args.profile})
    if detail.status_code == 200:
        where = detail.json()["path"]
    for e in r.json()["error"].get("details") or []:
        pos = ":".join(str(x) for x in (e.get("line"), e.get("column")) if x is not None)
        prefix = f"{where}:{pos}" if pos else where
        print(f"{prefix}: {e['path']}: {e['message']}", file=sys.stderr)


def format_event(event: dict[str, Any], workflow: str, profile: str) -> str | None:
    """One human-readable line per event, or None for events not worth a line (CLI-8)."""
    ts = datetime.fromisoformat(event["ts"]).astimezone().strftime("%H:%M:%S")
    type_, data, step = event["type"], event.get("data") or {}, event.get("step_id")
    run = f"run {event['run_id']}"
    if type_ == "run.started":
        return f"{ts} {run} started ({workflow} @ {profile})"
    if type_ == "run.finished":
        error = f": {data['error']}" if data.get("error") else ""
        return f"{ts} {run} {data.get('status', 'finished')}{error}"
    if type_ == "run.step.log":
        target = f" {data['target']}" if data.get("target") else ""
        stream = data.get("stream", "info")
        prefix = "" if stream == "info" else f"{stream}: "
        return f"{ts} [{step}]{target} {prefix}{data.get('line', '')}"
    if type_ == "run.step.finished":
        status = data.get("status", "")
        if status == "succeeded":
            return None
        error = f": {data['error']}" if data.get("error") else ""
        return f"{ts} [{step}] {status}{error}"
    return None


def cmd_run(args: argparse.Namespace) -> int:
    try:
        run_vars = parse_vars(args.var)
    except ValueError as exc:
        _err(str(exc))
        return EXIT_USAGE
    try:
        client = connect()
    except NoServerError as exc:
        _err(str(exc))
        return EXIT_NO_SERVER
    try:
        return _run(client, args, run_vars)
    except httpx.TransportError as exc:
        _err(f"lost the connection to the server: {exc}")
        return EXIT_NO_SERVER
    finally:
        client.close()


def _run(client: Client, args: argparse.Namespace, run_vars: dict[str, str]) -> int:
    body: dict[str, Any] = {"profile": args.profile, "workflow": args.workflow, "vars": run_vars}
    if args.input is not None:
        body["input"] = str(args.input.resolve())
    r = client.post("/api/runs", body)
    if r.status_code == 422 and r.json()["error"]["code"] == "validation_failed":
        _err(_message(r))
        if r.json()["error"].get("details"):
            _print_validation(client, args, r)
        return EXIT_USAGE
    if r.status_code != 201:
        _err(_message(r))
        return EXIT_USAGE if r.status_code < 500 else EXIT_FAILED
    run_id = str(r.json()["id"])
    follow = args.follow if args.follow is not None else (args.json or sys.stdout.isatty())
    if not follow:
        print(run_id)
        return EXIT_OK
    return follow_run(client, run_id, args)


def follow_run(client: Client, run_id: str, args: argparse.Namespace) -> int:
    """Stream events until run.finished. First signal cancels, a second exits (CLI-9)."""
    state: dict[str, int | None] = {"signal": None}

    def on_signal(signum: int, frame: FrameType | None) -> None:
        if state["signal"] is None:
            state["signal"] = signum
            raise _Cancel(signum)
        raise _Abort(signum)

    previous = {s: signal.signal(s, on_signal) for s in EXIT_SIGNAL}
    last, refused, status, finished = 0, False, "failed", False
    try:
        while not finished:
            try:
                for _, event in client.sse(f"/api/runs/{run_id}/events", last):
                    last = int(event["seq"])
                    data = event.get("data") or {}
                    if event["type"] == "run.step.finished" and data.get("refused"):
                        refused = True
                    if args.json:
                        print(json.dumps(event), flush=True)
                    else:
                        line = format_event(event, args.workflow, args.profile)
                        if line is not None:
                            print(line, flush=True)
                    if event["type"] == "run.finished":
                        status, finished = str(data.get("status", "failed")), True
                        break
                else:
                    if not finished:  # the stream ended early (server restart?)
                        _err("the event stream ended before the run finished")
                        return EXIT_NO_SERVER
            except _Cancel:
                _err("cancelling the run (press Ctrl-C again to stop waiting)")
                client.post(f"/api/runs/{run_id}/cancel")
                # reconnect after the last event we saw
    except _Abort as exc:
        _err(f"stopped waiting; run {run_id} may still be running")
        return EXIT_SIGNAL[signal.Signals(exc.signum)]
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
    if refused:
        _err(
            "a step refused to run. If it needs trust, review the workflow with "
            "`rifferr validate --safety <file>`, then run `rifferr trust <file>`."
        )
    if state["signal"] is not None:
        return EXIT_SIGNAL[signal.Signals(state["signal"])]
    return EXIT_OK if status == "succeeded" else EXIT_FAILED
