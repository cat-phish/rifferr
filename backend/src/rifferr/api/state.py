"""Server-side state shared by the routes: app DB, supervisor, step registry, event hub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from rifferr.appdb import AppDB
from rifferr.engine import Event, StepTypeRegistry
from rifferr.worker.previews import PreviewSupervisor
from rifferr.worker.supervisor import WorkerSupervisor

RUN_LEVEL_EVENTS = frozenset({"run.started", "run.finished"})


class EventHub:
    """Fans persisted events out to SSE subscribers (API-8, API-9)."""

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[Event], str | None] = {}

    def subscribe(self, run_id: str | None) -> asyncio.Queue[Event]:
        """``run_id=None``: run-level events of every run."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[queue] = run_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.pop(queue, None)

    def publish(self, event: Event) -> None:
        for queue, run_id in list(self._subscribers.items()):
            if run_id == event.run_id or (run_id is None and event.type in RUN_LEVEL_EVENTS):
                queue.put_nowait(event)


@dataclass
class ServerState:
    config_root: Path
    db: AppDB
    supervisor: WorkerSupervisor
    previews: PreviewSupervisor
    registry: StepTypeRegistry
    keepalive: float
    hub: EventHub = field(default_factory=EventHub)


def server_state(request: Request) -> ServerState:
    state: ServerState = request.app.state.rifferr
    return state


State = Annotated[ServerState, Depends(server_state)]
"""Route parameter type for the shared server state."""
