"""Messages on the server <-> worker pipes (WORKER-9): one JSON object per line.

Internal: not a published schema.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from rifferr.engine.events import Event

HEARTBEAT_INTERVAL = 5.0
"""Seconds between worker heartbeats (WORKER-10)."""
HUNG_AFTER = 30.0
"""Seconds of silence after which the server treats a worker as hung (WORKER-10)."""
MAX_MESSAGE = 16 * 1024 * 1024


class WorkerCommand(BaseModel):
    """Server -> worker. ``start_run`` only wakes the worker: the job is in the job table.

    ``request`` / ``cancel_request`` go to the read-only worker (WORKER-11): short-lived
    requests that are not stored anywhere and answer with a ``response`` message.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["start_run", "cancel_run", "stop", "request", "cancel_request"]
    profile: str
    run_id: str | None = None
    request_id: str | None = None
    request: dict[str, JsonValue] | None = None


class WorkerMessage(BaseModel):
    """Worker -> server."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ready", "heartbeat", "event", "error", "response"]
    event: Event | None = None
    message: str | None = None
    request_id: str | None = None
    result: dict[str, JsonValue] | None = None
    """A ``response``'s answer; absent when it failed (``error_code``, ``message``)."""
    error_code: str | None = None


def encode(message: WorkerCommand | WorkerMessage) -> bytes:
    return message.model_dump_json(exclude_none=True).encode() + b"\n"
