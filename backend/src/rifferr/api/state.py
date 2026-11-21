"""Server-side state shared by the routes: app DB, supervisor, step registry, event hub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Request

from rifferr.appdb import AppDB
from rifferr.covers.service import CoverService
from rifferr.engine import Event, StepTypeRegistry
from rifferr.engine.events import PROFILE_STREAM_PREFIX
from rifferr.notify.service import NotificationService
from rifferr.triggers import TriggerManager
from rifferr.worker.previews import PreviewSupervisor
from rifferr.worker.supervisor import WorkerSupervisor

RUN_LEVEL_EVENTS = frozenset({"run.started", "run.finished"})
#: Also on ``/api/events``, so the review queue updates without polling (REVIEW-QUEUE-10).
REVIEW_EVENTS = frozenset(
    {
        "run.waiting_review",
        "run.resumed",
        "review.requested",
        "review.resolved",
        "review.decided",
        "import.requested",
        "import.resolved",
        "cleanup.requested",
        "cleanup.resolved",
        "cover.requested",
        "cover.resolved",
        "move.requested",
        "move.resolved",
        "job.finished",
    }
)
#: Lane state changes (C-LANES LANES-2) go on ``/api/events`` too.
LANE_EVENTS = frozenset({"lane.started", "lane.waiting", "lane.resumed", "lane.finished"})


def on_all_stream(event: Event) -> bool:
    """Whether ``GET /api/events`` carries ``event`` (API-9, REVIEW-QUEUE-10): run-level
    and review-level events and the ``changeset.*`` events of runs, plus the
    ``trigger.*`` events of profile streams (TRIGGERS-24) and ``lane.*`` (LANES-2)."""
    if event.type.startswith(
        ("trigger.", "notify.", "inbox.", "covers.")
    ):  # TRIGGERS-24, NOTIFY-4, DASHBOARD-15, COVERS-SEARCH-19
        return True
    if event.run_id.startswith(PROFILE_STREAM_PREFIX):
        return False
    return (
        event.type in RUN_LEVEL_EVENTS
        or event.type in REVIEW_EVENTS
        or event.type in LANE_EVENTS
        or event.type.startswith("changeset.")
    )


class EventHub:
    """Fans persisted events out to SSE subscribers (API-8, API-9)."""

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue[Event], str | None] = {}
        self._closed = asyncio.Event()

    def subscribe(self, run_id: str | None) -> asyncio.Queue[Event]:
        """``run_id=None``: the events of every run that ``on_all_stream`` picks."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[queue] = run_id
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.pop(queue, None)

    def close(self) -> None:
        """The server is shutting down: every stream ends (CLI-6, B-068). An open stream
        would otherwise keep its connection, and with it the shutdown, waiting forever."""
        self._closed.set()

    async def next(self, queue: asyncio.Queue[Event], timeout: float) -> Event | None:
        """The next event on ``queue``, or ``None`` once the hub is closed. Raises
        ``TimeoutError`` if neither happens within ``timeout`` seconds."""
        if self._closed.is_set():
            return None
        get = asyncio.ensure_future(queue.get())
        stop = asyncio.ensure_future(self._closed.wait())
        try:
            done, _ = await asyncio.wait(
                {get, stop}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (get, stop):
                if not task.done():
                    task.cancel()
        if get in done:
            return get.result()
        if stop in done:
            return None
        raise TimeoutError

    def publish(self, event: Event) -> None:
        for queue, run_id in list(self._subscribers.items()):
            if run_id == event.run_id or (run_id is None and on_all_stream(event)):
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
    triggers: TriggerManager | None = None
    notifications: NotificationService | None = None
    covers: CoverService = field(default_factory=lambda: CoverService())


def server_state(request: Request) -> ServerState:
    state: ServerState = request.app.state.rifferr
    return state


State = Annotated[ServerState, Depends(server_state)]
"""Route parameter type for the shared server state."""
