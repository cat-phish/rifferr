"""Lanes: one album's path through a run (C-LANES).

A run executes steps in passes; each pass of a step acts on the active lanes that have
reached it. Holds (pending ChangeSets, review gates, ``ctx.hold``) pause only the lanes
they are about.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from pydantic import JsonValue

LaneState = Literal["active", "waiting", "done", "failed", "rejected", "gone", "cancelled"]
HoldKind = Literal["changeset", "gate", "import"]

FINAL_LANE_STATES: Final = frozenset({"done", "failed", "rejected", "gone", "cancelled"})
PENDING_PREFIX: Final = "pending:"


@dataclass(frozen=True)
class Hold:
    kind: HoldKind
    ref: str

    def as_json(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "ref": self.ref}


@dataclass
class Lane:
    """LANES-2. ``position`` is the id of the next step to run; ``None`` once past the
    last step (or final)."""

    key: str
    label: str
    state: LaneState = "active"
    position: str | None = None
    holds: list[Hold] = field(default_factory=list[Hold])
    steps: dict[str, str] = field(default_factory=dict[str, str])
    """Step id -> this lane's status in the pass of that step that included it."""
    passes: dict[str, int] = field(default_factory=dict[str, int])
    """Step id -> the pass number of that step that included this lane (LANES-4)."""

    @property
    def final(self) -> bool:
        return self.state in FINAL_LANE_STATES

    @property
    def pending(self) -> bool:
        """A lane for an album that isn't imported yet (LANES-14)."""
        return self.key.startswith(PENDING_PREFIX)


class LaneStore(Protocol):
    """Where a run's lanes are kept between passes and across a suspension."""

    def load(self, run_id: str) -> dict[str, Lane]: ...

    def save(self, run_id: str, lanes: Iterable[Lane]) -> None: ...

    def rename(self, run_id: str, old: str, new: str) -> None: ...

    def resolve_hold(self, run_id: str, ref: str) -> None: ...

    def resolved_holds(self, run_id: str) -> list[str]: ...


class InMemoryLaneStore:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Lane]] = {}
        self._resolved: dict[str, set[str]] = {}

    def load(self, run_id: str) -> dict[str, Lane]:
        return {k: _copy(v) for k, v in self._runs.get(run_id, {}).items()}

    def save(self, run_id: str, lanes: Iterable[Lane]) -> None:
        stored = self._runs.setdefault(run_id, {})
        for lane in lanes:
            stored[lane.key] = _copy(lane)

    def rename(self, run_id: str, old: str, new: str) -> None:
        stored = self._runs.get(run_id, {})
        if old in stored:
            lane = stored.pop(old)
            lane.key = new
            stored[new] = lane

    def resolve_hold(self, run_id: str, ref: str) -> None:
        self._resolved.setdefault(run_id, set()).add(ref)

    def resolved_holds(self, run_id: str) -> list[str]:
        return sorted(self._resolved.get(run_id, set()))


def _copy(lane: Lane) -> Lane:
    return Lane(
        lane.key,
        lane.label,
        lane.state,
        lane.position,
        list(lane.holds),
        dict(lane.steps),
        dict(lane.passes),
    )


def lane_json(lane: Lane) -> dict[str, JsonValue]:
    return {
        "key": lane.key,
        "label": lane.label,
        "state": lane.state,
        "position": lane.position,
        "holds": [h.as_json() for h in lane.holds],
    }


def counts(lanes: Iterable[Lane]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lane in lanes:
        out[lane.state] = out.get(lane.state, 0) + 1
    return out
