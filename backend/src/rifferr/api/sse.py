"""Server-Sent Events (API-8, API-9). The UI never polls."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

from rifferr.api.state import ServerState
from rifferr.engine import Event

STREAM_HEADERS = {"cache-control": "no-cache", "x-accel-buffering": "no"}


def format_event(event: Event) -> str:
    return f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json()}\n\n"


async def run_events(state: ServerState, run_id: str, after: int) -> AsyncGenerator[str]:
    """Replay persisted events after ``after``, then live ones; end after run.finished."""
    queue = state.hub.subscribe(run_id)  # before replay, so nothing slips between the two
    try:
        last = after
        for event in state.db.events_since(run_id, after):
            yield format_event(event)
            last = event.seq
            if event.type == "run.finished":
                return
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), state.keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            if event.seq <= last:
                continue
            yield format_event(event)
            last = event.seq
            if event.type == "run.finished":
                return
    finally:
        state.hub.unsubscribe(queue)


async def all_run_events(state: ServerState) -> AsyncGenerator[str]:
    """run.started / run.finished of every run, live only."""
    queue = state.hub.subscribe(None)
    try:
        yield ": connected\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), state.keepalive)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
    finally:
        state.hub.unsubscribe(queue)
