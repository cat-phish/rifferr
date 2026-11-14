"""Run events and the sink that persists them (RUN-6, RUN-7)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue

#: Event types the engine owns. Steps emit other ``run.step.*`` types.
ENGINE_EVENTS = frozenset(
    {
        "run.started",
        "run.resumed",
        "run.step.started",
        "run.step.finished",
        "run.waiting_review",
        "run.finished",
        # C-CHANGESET (CHANGESET-5)
        "changeset.proposed",
        "changeset.applied",
        "changeset.rejected",
        "changeset.conflicted",
        "changeset.undone",
        # S-REVIEW
        "review.requested",
        "review.resolved",
        # C-LANES (LANES-2)
        "lane.started",
        "lane.waiting",
        "lane.resumed",
        "lane.finished",
        # F-TAG-GRID: a job without a run finished (profile stream)
        "job.finished",
    }
)

PROFILE_STREAM_PREFIX = "profile:"


def profile_stream(profile: str) -> str:
    """The event stream of a profile's work that has no run: manual ChangeSets and
    their undo (TAG-GRID-9). Stored like a run's stream, under this id."""
    return PROFILE_STREAM_PREFIX + profile


class Event(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    ts: datetime
    run_id: str
    type: str
    step_id: str | None = None
    data: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class EventSink(Protocol):
    """Durable event storage. ``append`` must persist before returning."""

    def append(self, event: Event) -> None: ...

    def since(self, run_id: str, seq: int) -> Iterable[Event]:
        """Events of ``run_id`` with ``seq`` greater than ``seq``, in order."""
        ...


class InMemoryEventSink:
    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = {}

    def append(self, event: Event) -> None:
        events = self._events.setdefault(event.run_id, [])
        expected = len(events) + 1
        if event.seq != expected:
            raise ValueError(f"run {event.run_id}: expected seq {expected}, got {event.seq}")
        events.append(event)

    def since(self, run_id: str, seq: int) -> Iterable[Event]:
        return list(self._events.get(run_id, [])[max(seq, 0) :])
