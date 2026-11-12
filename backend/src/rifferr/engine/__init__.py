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
    "Clock",
    "Engine",
    "Event",
    "EventSink",
    "FailedTarget",
    "InMemoryEventSink",
    "InvalidTransitionError",
    "NoSecrets",
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
]
