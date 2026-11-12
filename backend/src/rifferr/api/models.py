"""Request and response bodies of the HTTP API (API-10). Not published schemas beyond
the OpenAPI document."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rifferr.appdb import JobState


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(_Model):
    code: str
    message: str
    details: JsonValue | None = None


class ErrorEnvelope(_Model):
    error: ErrorBody


class Health(_Model):
    status: Literal["ok"] = "ok"


class ProfileSummary(_Model):
    name: str
    backend: str | None = None
    vars: list[str] = Field(default_factory=list[str], description="Variable names only")
    secrets: list[str] = Field(default_factory=list[str], description="Secret names only")
    error: str | None = Field(default=None, description="Why the profile can't be loaded")


class WorkflowError(_Model):
    path: str
    message: str
    line: int | None = None
    column: int | None = None


class VarInfo(_Model):
    name: str
    default: str | None = None
    description: str | None = None
    profile_value: str | None = Field(default=None, description="Set by the profile's vars")


class WorkflowSummary(_Model):
    name: str
    path: str
    sha256: str
    valid: bool
    trusted: bool
    vars: list[VarInfo] = Field(default_factory=list[VarInfo])
    missing_vars: list[str] = Field(
        default_factory=list[str], description="Vars to supply at start (API-16)"
    )


class WorkflowDetail(WorkflowSummary):
    yaml: str
    errors: list[WorkflowError]


class SafetyFinding(_Model):
    step_id: str
    kind: str
    detail: str


class ValidateResult(_Model):
    valid: bool
    errors: list[WorkflowError]
    safety: list[SafetyFinding]
    sha256: str
    trusted: bool


class TrustRequest(_Model):
    sha256: str = Field(description="Hash of the content the user reviewed")


class TrustResult(_Model):
    name: str
    sha256: str
    trusted: Literal[True] = True


class RunCreate(_Model):
    profile: str
    workflow: str = Field(description="Workflow file stem in the profile's workflows/ folder")
    vars: dict[str, str] = Field(default_factory=dict[str, str])
    sha256: str | None = Field(
        default=None, description="Start only if the file still has this hash (API-17)"
    )


class RunCreated(_Model):
    id: str


class RunSummary(_Model):
    id: str
    profile: str
    workflow: str | None
    workflow_hash: str | None
    state: JobState
    trusted: bool
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


StepStatusOut = Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled"]


class StepState(_Model):
    id: str
    uses: str
    status: StepStatusOut
    error: str | None = None
    outputs: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class RunDetail(RunSummary):
    steps: list[StepState]


class Accepted(_Model):
    id: str
    status: Literal["cancel_requested"] = "cancel_requested"


class ScriptError(_Model):
    """A problem in a script file, with its YAML location (TAGSCRIPT-2)."""

    path: str
    message: str
    line: int | None = None
    column: int | None = None


class ScriptSummary(_Model):
    """A file in the profile's ``scripts/`` folder (API-18)."""

    name: str
    file: str
    sha256: str
    valid: bool
    errors: list[ScriptError]


class ScriptDetail(ScriptSummary):
    text: str


class ScriptSave(_Model):
    text: str
    sha256: str | None = Field(
        default=None,
        description="Hash of the text the editor loaded; 409 hash_mismatch if the file changed",
    )
