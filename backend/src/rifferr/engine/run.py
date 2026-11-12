"""Run and step records with the run state machine (RUN-1, RUN-11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Literal

Scalar = str | int | float | bool
StepStatus = Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled"]


class RunState(StrEnum):
    queued = "queued"
    running = "running"
    waiting_review = "waiting_review"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.queued: frozenset({RunState.running, RunState.cancelled}),
    RunState.running: frozenset(
        {RunState.succeeded, RunState.failed, RunState.cancelled, RunState.waiting_review}
    ),
    # RUN-14: suspended on holds (pending ChangeSets, review gates), no worker held.
    RunState.waiting_review: frozenset({RunState.queued, RunState.cancelled}),
    RunState.succeeded: frozenset(),
    RunState.failed: frozenset(),
    RunState.cancelled: frozenset(),
}
FINISHED = frozenset({RunState.succeeded, RunState.failed, RunState.cancelled})


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FailedTarget:
    target: str
    message: str


@dataclass
class StepRecord:
    id: str
    uses: str
    status: StepStatus = "pending"
    outputs: dict[str, Scalar] = field(default_factory=dict[str, Scalar])
    failed_targets: list[FailedTarget] = field(default_factory=list[FailedTarget])
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass
class Run:
    id: str
    workflow: str
    workflow_hash: str
    profile: str
    created_at: datetime
    steps: list[StepRecord]
    vars: dict[str, str]
    trusted: bool = False
    state: RunState = RunState.queued
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_seq: int = 0
    suspended_after: str | None = None
    """Step id the run is suspended after while ``waiting_review`` (RUN-14)."""
    holds: list[str] = field(default_factory=list[str])
    """ChangeSet ids the suspended run waits for (RUN-14)."""

    def transition(self, to: RunState) -> None:
        if to not in _ALLOWED[self.state]:
            raise InvalidTransitionError(f"run {self.id}: can't go from {self.state} to {to}")
        self.state = to

    @property
    def finished(self) -> bool:
        return self.state in FINISHED
