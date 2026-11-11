"""Workflow files: models, loading, validation, interpolation, schema (C-WORKFLOW)."""

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
from rifferr.core.workflow.schema import workflow_json_schema
from rifferr.core.workflow.validation import validate
from rifferr.core.workflow.yamldoc import RoundTripDoc

#: File extensions recognized as workflow files; default location is <profile>/workflows/.
WORKFLOW_SUFFIXES = (".yaml", ".yml")

__all__ = [
    "PROFILE_FIELDS",
    "ROOTS",
    "RUN_FIELDS",
    "WORKFLOW_SUFFIXES",
    "Expression",
    "InterpolationContext",
    "InterpolationError",
    "RoundTripDoc",
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
    "WorkflowLoadError",
    "find_expressions",
    "format_path",
    "interpolate",
    "load_workflow",
    "validate",
    "workflow_json_schema",
]
