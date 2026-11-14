"""Request and response bodies of the HTTP API (API-10). Not published schemas beyond
the OpenAPI document."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from rifferr.appdb import JobState
from rifferr.core.changeset import ChangeSet, ChangeSetState
from rifferr.core.imports import MAX_LOOKUP_IDS, MAX_LOOKUP_TEXT, ImportAction, ImportDecision
from rifferr.core.profile import GridColumns
from rifferr.core.review import ReviewGate
from rifferr.engine.changesets import CurrentTarget


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
    grid_columns: GridColumns = Field(
        default_factory=GridColumns, description="Default tag-grid columns (TAG-GRID-3)"
    )
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
    input: str | None = Field(
        default=None,
        description="Folder for {{ run.input }} (TRIGGERS-7); default: the workflow's input",
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
    trigger: Literal["manual", "watch", "schedule", "webhook"] = Field(
        default="manual", description="What started the run (TRIGGERS-24)"
    )
    input: str | None = Field(default=None, description="The input its trigger gave")
    lane_counts: dict[str, int] = Field(
        default_factory=dict[str, int],
        description="The run's lanes (one per album) counted by state (LANES-9)",
    )


LaneStateOut = Literal[
    "active", "waiting", "done", "skipped", "failed", "rejected", "gone", "cancelled"
]


class LaneHold(_Model):
    kind: Literal["changeset", "gate", "import"]
    ref: str = Field(description="ChangeSet id, review gate id or import decision id")


class LaneOut(_Model):
    """One album's path through the run (C-LANES LANES-15)."""

    key: str = Field(description="Backend album id, or pending:<id> before import")
    label: str
    state: LaneStateOut
    position: str | None = Field(description="Id of the next step; null once final")
    holds: list[LaneHold]
    steps: dict[str, str] = Field(description="Step id -> this lane's status in that step")


StepStatusOut = Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled"]


class StepState(_Model):
    id: str
    uses: str
    status: StepStatusOut
    error: str | None = None
    outputs: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    passes: int = Field(default=0, description="How many passes of this step ran (LANES-4)")


class RunDetail(RunSummary):
    steps: list[StepState]
    lanes: list[LaneOut] = Field(
        default_factory=list[LaneOut], description="The run's lanes in creation order (LANES-15)"
    )
    undoable: int = Field(
        default=0,
        description="The run's applied journal entries not undone yet; undo the run to "
        "revert them (REVIEW-QUEUE-15)",
    )


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


class ChangesCreated(_Model):
    """Manual ChangeSets queued for applying (TAG-GRID-8, -13)."""

    changesets: list[str]
    job_id: str
    queued_behind: list[str] = Field(
        description="Runs running or queued before the edit job; it applies after them"
    )


class ManualEdit(_Model):
    """A manual ChangeSet in the recent-edits list (TAG-GRID-11)."""

    changeset: ChangeSet
    state: ChangeSetState
    resolved_by: str | None = None
    journal_id: str | None = Field(
        default=None, description="Journal entry of the apply; undo it to revert the edit"
    )
    undo_journal_id: str | None = Field(
        default=None, description="Journal entry of the undo, once undone"
    )


class JobQueued(_Model):
    job_id: str


# -- review queue (F-REVIEW-QUEUE) ------------------------------------------------------


class QueueItem(_Model):
    """One thing that waits for a decision (REVIEW-QUEUE-1). ``kind`` says which of
    ``changeset`` / ``gate`` / ``import_decision`` is set (IMPORT-UI-7)."""

    kind: Literal["changeset", "gate", "import"]
    id: str
    profile: str
    run_id: str
    workflow: str | None = None
    step_id: str | None = None
    created_at: datetime
    changeset: ChangeSet | None = None
    state: ChangeSetState | None = None
    gate: ReviewGate | None = None
    import_decision: ImportDecision | None = None


class ChangeSetDetail(_Model):
    """A ChangeSet with its review state (REVIEW-QUEUE-2)."""

    changeset: ChangeSet
    state: ChangeSetState
    decisions: dict[int, bool] = Field(
        default_factory=dict[int, bool], description="Field-change index -> accepted"
    )
    resolved_by: str | None = None
    note: str | None = None
    journal_ids: list[str] = Field(default_factory=list[str])


class CurrentValues(_Model):
    """What a ChangeSet's targets hold now, from the read-only worker (REVIEW-QUEUE-3)."""

    targets: list[CurrentTarget]


class Decision(_Model):
    decision: Literal["accept", "reject"]
    accepted: list[int] | None = Field(
        default=None, description="Field-change indexes to accept; absent = all"
    )
    note: str | None = Field(default=None, max_length=2000)


class ImportDecisionBody(_Model):
    """How to answer an import decision (IMPORT-UI-8). For kind ``duplicate`` the
    action is one of ``skip`` / ``keep`` / ``remove`` / ``merge``."""

    action: ImportAction
    candidate_id: str | None = Field(default=None, max_length=MAX_LOOKUP_TEXT * 2)
    note: str | None = Field(default=None, max_length=2000)


class ImportLookupBody(_Model):
    """Look the folder up again (IMPORT-UI-9): by release ids, or by artist and album.
    Empty repeats the plain lookup."""

    ids: list[Annotated[str, StringConstraints(min_length=1, max_length=MAX_LOOKUP_TEXT)]] = Field(
        default_factory=list[str], max_length=MAX_LOOKUP_IDS
    )
    artist: str | None = Field(default=None, max_length=MAX_LOOKUP_TEXT)
    album: str | None = Field(default=None, max_length=MAX_LOOKUP_TEXT)


class GateDecisionBody(_Model):
    decision: Literal["continue", "reject"]
    note: str | None = Field(default=None, max_length=2000)


class RunDecision(_Model):
    decision: Literal["accept", "reject"]


class JobsQueued(_Model):
    job_ids: list[str]


# -- triggers (F-TRIGGERS) ------------------------------------------------------------


class TriggerStateOut(_Model):
    """One trigger of a workflow (TRIGGERS-4)."""

    workflow: str
    kind: Literal["watch", "schedule", "webhook"]
    state: Literal["armed", "disabled", "error"]
    detail: str | None = Field(description="Why it is disabled or failing")
    path: str | None = Field(description="The resolved watch folder")
    next_fire: datetime | None = Field(description="The next schedule time")
    last_fired: datetime | None
    last_run_id: str | None
    webhook_name: str | None = Field(default=None, description="Webhooks: on.webhook.name")
    has_secret: bool | None = Field(
        default=None, description="Webhooks: whether a secret has been created"
    )


class WebhookSecret(_Model):
    """A new webhook secret. It is shown only in this response (TRIGGERS-17)."""

    profile: str
    name: str
    url_path: str
    secret: str


class HookAccepted(_Model):
    run_id: str | None = None
    skipped: str | None = None
    ignored: str | None = None
