"""Run state machine, step registry, events and the executor (C-RUN).

Pure: all I/O goes through injected interfaces.
"""

from rifferr.engine.events import ENGINE_EVENTS, Event, EventSink, InMemoryEventSink
from rifferr.engine.executor import Clock, Engine, RunValidationError, SystemClock
from rifferr.engine.journal import InMemoryJournal, Journal, JournalError
from rifferr.engine.lanes import (
    FINAL_LANE_STATES,
    Hold,
    InMemoryLaneStore,
    Lane,
    LaneState,
    LaneStore,
    counts,
    lane_json,
)
from rifferr.engine.run import (
    FailedTarget,
    InvalidTransitionError,
    PassRecord,
    Run,
    RunState,
    Scalar,
    StepRecord,
    StepStatus,
)
from rifferr.engine.steps import (
    NoSecrets,
    SecretAccess,
    StepContext,
    StepFailure,
    StepRefused,
    StepResult,
    StepType,
    StepTypeRegistry,
)

__all__ = [
    "ENGINE_EVENTS",
    "FINAL_LANE_STATES",
    "Clock",
    "Engine",
    "Event",
    "EventSink",
    "FailedTarget",
    "Hold",
    "InMemoryEventSink",
    "InMemoryJournal",
    "InMemoryLaneStore",
    "InvalidTransitionError",
    "Journal",
    "JournalError",
    "Lane",
    "LaneState",
    "LaneStore",
    "NoSecrets",
    "PassRecord",
    "Run",
    "RunState",
    "RunValidationError",
    "Scalar",
    "SecretAccess",
    "StepContext",
    "StepFailure",
    "StepRecord",
    "StepRefused",
    "StepResult",
    "StepStatus",
    "StepType",
    "StepTypeRegistry",
    "SystemClock",
    "counts",
    "lane_json",
]
