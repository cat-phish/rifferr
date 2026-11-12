"""Run state machine, step registry, events and the executor (C-RUN).

Pure: all I/O goes through injected interfaces.
"""

from rifferr.engine.events import ENGINE_EVENTS, Event, EventSink, InMemoryEventSink
from rifferr.engine.executor import Clock, Engine, RunValidationError, SystemClock
from rifferr.engine.run import (
    FailedTarget,
    InvalidTransitionError,
    Run,
    RunState,
    Scalar,
    StepRecord,
    StepStatus,
)
from rifferr.engine.steps import StepContext, StepFailure, StepResult, StepType, StepTypeRegistry

__all__ = [
    "ENGINE_EVENTS",
    "Clock",
    "Engine",
    "Event",
    "EventSink",
    "FailedTarget",
    "InMemoryEventSink",
    "InvalidTransitionError",
    "Run",
    "RunState",
    "RunValidationError",
    "Scalar",
    "StepContext",
    "StepFailure",
    "StepRecord",
    "StepResult",
    "StepStatus",
    "StepType",
    "StepTypeRegistry",
    "SystemClock",
]
