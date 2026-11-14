"""Workflow files: models, loading, validation, interpolation, schema (C-WORKFLOW)."""

from rifferr.core.workflow.calls import (
    CALL_TYPE,
    CalledStep,
    Calls,
    WorkflowCallParams,
    expand,
    resolve_calls,
    stored_reader,
)
from rifferr.core.workflow.errors import ValidationError, WorkflowLoadError, format_path
from rifferr.core.workflow.interpolation import (
    PROFILE_FIELDS,
    ROOTS,
    RUN_FIELDS,
    Expression,
    InterpolationContext,
    InterpolationError,
    find_expressions,
    interpolate,
    substitute,
)
from rifferr.core.workflow.loader import load_workflow
from rifferr.core.workflow.models import (
    StepSpec,
    Triggers,
    VarDecl,
    WatchTrigger,
    WebhookTrigger,
    Workflow,
)
from rifferr.core.workflow.registry import StaticStepRegistry, StepRegistry, StepTypeInfo
from rifferr.core.workflow.safety import Finding, SafetyReport, safety_report
from rifferr.core.workflow.schema import workflow_json_schema
from rifferr.core.workflow.validation import validate
from rifferr.core.workflow.yamldoc import RoundTripDoc

#: File extensions recognized as workflow files; default location is <profile>/workflows/.
WORKFLOW_SUFFIXES = (".yaml", ".yml")

__all__ = [
    "CALL_TYPE",
    "PROFILE_FIELDS",
    "ROOTS",
    "RUN_FIELDS",
    "WORKFLOW_SUFFIXES",
    "CalledStep",
    "Calls",
    "Expression",
    "Finding",
    "InterpolationContext",
    "InterpolationError",
    "RoundTripDoc",
    "SafetyReport",
    "StaticStepRegistry",
    "StepRegistry",
    "StepSpec",
    "StepTypeInfo",
    "Triggers",
    "ValidationError",
    "VarDecl",
    "WatchTrigger",
    "WebhookTrigger",
    "Workflow",
    "WorkflowCallParams",
    "WorkflowLoadError",
    "expand",
    "find_expressions",
    "format_path",
    "interpolate",
    "load_workflow",
    "resolve_calls",
    "safety_report",
    "stored_reader",
    "substitute",
    "validate",
    "workflow_json_schema",
]
