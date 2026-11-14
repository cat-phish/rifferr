"""Workflow file models (C-WORKFLOW)."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rifferr.core.cron import CronError, parse_cron

STEP_ID_PATTERN = r"^[a-z][a-z0-9_-]*$"
STEP_TYPE_PATTERN = r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)*$"
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]*$"
DURATION_PATTERN = r"^[1-9][0-9]*[smh]$"
#: A dotted path into a JSON document; numeric parts index lists (``trackFiles.0.path``).
JSON_PATH_PATTERN = r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$"

Duration = Annotated[str, Field(pattern=DURATION_PATTERN, description="<n>s, <n>m or <n>h")]


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
    """``on.watch`` (F-TRIGGERS TRIGGERS-8 to -11)."""

    path: str = Field(description="Folder to watch; each top-level folder in it is one input.")
    settle: Duration = Field(
        default="2m", description="How long an input must stay unchanged before a run starts."
    )
    poll: Duration | None = Field(
        default=None, description="Poll at this interval instead of using file events."
    )

    @field_validator("poll")
    @classmethod
    def _poll_floor(cls, value: str | None) -> str | None:
        if value is not None and value.endswith("s") and int(value[:-1]) < 5:
            raise ValueError("poll interval must be at least 5s")
        return value


class WebhookTrigger(_Strict):
    """``on.webhook`` (F-TRIGGERS TRIGGERS-15 to -18)."""

    name: Annotated[str, Field(pattern=NAME_PATTERN)]
    input: Annotated[str, Field(pattern=JSON_PATH_PATTERN)] = Field(
        description="Dotted path to the input path in the JSON body, e.g. trackFiles.0.path."
    )
    within: str | None = Field(
        default=None,
        description="Inputs must lie inside this folder (default: on.watch.path).",
    )


class Triggers(_Strict):
    """The ``on:`` block (WORKFLOW-13, F-TRIGGERS)."""

    watch: WatchTrigger | None = None
    schedule: str | None = Field(
        default=None, description="Five-field cron expression or @daily-style alias."
    )
    webhook: WebhookTrigger | None = None

    @field_validator("schedule")
    @classmethod
    def _cron(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                parse_cron(value)
            except CronError as exc:
                raise ValueError(str(exc)) from None
        return value

    @model_validator(mode="after")
    def _webhook_scope(self) -> Triggers:
        if self.webhook is not None and self.webhook.within is None and self.watch is None:
            raise ValueError("on.webhook needs 'within' (or an on.watch path to default to)")
        return self


class Workflow(_Strict):
    """A workflow file (WORKFLOW-1)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, title="Workflow")

    name: Annotated[str, Field(min_length=1)]
    description: str | None = None
    vars: dict[str, VarDecl] = Field(default_factory=dict[str, VarDecl])
    input: str | None = Field(
        default=None,
        description="Default {{ run.input }} for runs whose trigger gives none (schedule, manual).",
    )
    on: Triggers | None = None
    steps: Annotated[list[StepSpec], Field(min_length=1)]
