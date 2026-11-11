"""Workflow file models (C-WORKFLOW)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

STEP_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
STEP_TYPE_PATTERN = r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StepSpec(_Strict):
    """One entry of ``steps:`` (WORKFLOW-2, WORKFLOW-3)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="Step")

    id: Annotated[str, Field(pattern=STEP_ID_PATTERN)]
    uses: Annotated[str, Field(pattern=STEP_TYPE_PATTERN)]
    name: str | None = None
    with_: dict[str, Any] = Field(default_factory=dict[str, Any], alias="with")
    when: str | None = Field(default=None, description="Backend condition (a beets query).")
    apply: Literal["auto", "review"] = Field(
        default="review",
        description="Whether this step's ChangeSets apply automatically or wait for review. "
        "Ignored by steps that don't produce ChangeSets.",
    )
    on_error: Literal["stop", "skip", "continue"] = "stop"


class VarDecl(_Strict):
    """A declared workflow variable (WORKFLOW-7)."""

    default: str | None = None
    description: str | None = None


class WatchTrigger(_Strict):
    path: str
    settle: str | None = None


class WebhookTrigger(_Strict):
    name: Annotated[str, Field(pattern=NAME_PATTERN)]


class Triggers(_Strict):
    """The ``on:`` block. Parsed and validated; not acted on until F-TRIGGERS (WORKFLOW-13)."""

    watch: WatchTrigger | None = None
    schedule: str | None = Field(default=None, description="cron expression")
    webhook: WebhookTrigger | None = None


class Workflow(_Strict):
    """A workflow file (WORKFLOW-1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="Workflow")

    name: Annotated[str, Field(min_length=1)]
    description: str | None = None
    vars: dict[str, VarDecl] = Field(default_factory=dict[str, VarDecl])
    on: Triggers | None = None
    steps: Annotated[list[StepSpec], Field(min_length=1)]
