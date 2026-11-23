"""Request and response bodies of the HTTP API (API-10). Not published schemas beyond
the OpenAPI document."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from rifferr.appdb import JobState
from rifferr.core.changeset import ChangeSet, ChangeSetState
from rifferr.core.covers import CoverChoice, CoverQuery, CoverReview, RankedCandidate, SearchLink
from rifferr.core.files import CleanupChoice, CleanupDecision
from rifferr.core.imports import (
    MAX_LOOKUP_IDS,
    MAX_LOOKUP_TEXT,
    FolderItem,
    ImportAction,
    ImportDecision,
)
from rifferr.core.moves import MoveChoice, MoveConflict
from rifferr.core.profile import GridColumns
from rifferr.core.prune import PruneChoice, PruneDecision
from rifferr.core.review import ReviewGate
from rifferr.core.snapshot import FieldValue
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


class NotifyTestBody(_Model):
    target: str = Field(description="A target name from the profile's notify.targets")


class NotifyTestResult(_Model):
    """S-NOTIFY NOTIFY-14. ``error`` never contains the target's URL."""

    target: str
    sent: bool
    error: str | None = None


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


class CalledWorkflow(_Model):
    """A workflow file a workflow calls (S-SUBWORKFLOW), trusted on its own (SUBWORKFLOW-13)."""

    path: str = Field(description="Relative to the profile's workflows/ folder")
    sha256: str
    trusted: bool
    yaml: str


class WorkflowStepInfo(_Model):
    id: str = Field(description="Step id; a called workflow's steps as <call id>/<step id>")
    uses: str
    may_wait: bool = Field(
        description="The step can put something in the review queue (UI-REFRESH-13)"
    )


class WorkflowDetail(WorkflowSummary):
    yaml: str
    errors: list[WorkflowError]
    steps: list[WorkflowStepInfo] = Field(
        default_factory=list[WorkflowStepInfo],
        description="Steps in run order, calls expanded; empty when the file doesn't load",
    )
    called: list[CalledWorkflow] = Field(
        default_factory=list[CalledWorkflow],
        description="Files it calls; 'trusted' is true only when they all are (SUBWORKFLOW-13)",
    )


class SafetyFinding(_Model):
    step_id: str
    kind: str
    detail: str
    workflow: str | None = Field(
        default=None, description="The called file the step comes from (SUBWORKFLOW-14)"
    )


class ValidateResult(_Model):
    valid: bool
    errors: list[WorkflowError]
    safety: list[SafetyFinding]
    sha256: str
    trusted: bool
    called: list[CalledWorkflow] = Field(default_factory=list[CalledWorkflow])


class TrustRequest(_Model):
    sha256: str = Field(description="Hash of the content the user reviewed")
    called: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Called file -> hash reviewed; every untrusted called file must be "
        "listed (SUBWORKFLOW-13)",
    )


class TrustResult(_Model):
    name: str
    sha256: str
    trusted: Literal[True] = True
    called: dict[str, str] = Field(default_factory=dict[str, str])


class BeetsConfigStatus(_Model):
    """SEC-62: what a profile's beets config runs, and whether the user trusted it."""

    sha256: str
    files: list[str]
    code_keys: list[str] = Field(description="Keys that run code, e.g. `pluginpath`")
    needs_trust: bool
    trusted: bool


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
    called_hashes: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Called workflow file -> hash of the text the run uses (SUBWORKFLOW-15)",
    )
    lane_counts: dict[str, int] = Field(
        default_factory=dict[str, int],
        description="The run's lanes (one per album) counted by state (LANES-9)",
    )


LaneStateOut = Literal[
    "active", "waiting", "done", "skipped", "failed", "rejected", "gone", "cancelled"
]


class LaneHold(_Model):
    kind: Literal["changeset", "gate", "import", "cleanup", "cover", "move", "prune"]
    ref: str = Field(
        description="ChangeSet id, review gate id, import decision id, cleanup decision id,"
        " prune decision id,"
        " cover review id or move conflict id"
    )


class LaneOut(_Model):
    """One album's path through the run (C-LANES LANES-15)."""

    key: str = Field(description="Backend album id, or pending:<id> before import")
    label: str
    state: LaneStateOut
    position: str | None = Field(description="Id of the next step; null once final")
    holds: list[LaneHold]
    steps: dict[str, str] = Field(description="Step id -> this lane's status in that step")


class PipelineLane(LaneOut):
    """A live album of a workflow and the step it is at (UI-REFRESH-24)."""

    run_id: str
    updated_at: datetime = Field(description="When the lane last changed (reached its step)")


class PipelineStart(_Model):
    """A queued or running run with no lanes yet (UI-REFRESH-24)."""

    run_id: str
    state: JobState
    input: str | None
    created_at: datetime


class PipelineOut(_Model):
    lanes: list[PipelineLane]
    starting: list[PipelineStart]


StepStatusOut = Literal["pending", "running", "succeeded", "failed", "skipped", "cancelled"]


class StepState(_Model):
    id: str
    uses: str
    status: StepStatusOut
    error: str | None = None
    outputs: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    passes: int = Field(default=0, description="How many passes of this step ran (LANES-4)")
    call_path: list[str] = Field(
        default_factory=list[str],
        description="Ids of the workflow calls this step runs inside (SUBWORKFLOW-6)",
    )
    workflow: str | None = Field(
        default=None, description="The called file the step comes from (SUBWORKFLOW-10)"
    )
    workflow_name: str | None = Field(default=None, description="That file's name")


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


class UndoBody(_Model):
    """Optional body of an undo request (LIBRARY-MOVE-15)."""

    force: list[Annotated[str, StringConstraints(min_length=1, max_length=4096)]] = Field(
        default_factory=list,
        max_length=10_000,
        description="Library files to undo although they changed since they were moved; "
        "the undo report's changed_files names them",
    )


#: An undo request without a body undoes as before: nothing forced.
NO_UNDO_BODY = UndoBody()


# -- review queue (F-REVIEW-QUEUE) ------------------------------------------------------


class QueueItem(_Model):
    """One thing that waits for a decision (REVIEW-QUEUE-1). ``kind`` says which of
    ``changeset`` / ``gate`` / ``import_decision`` / ``cleanup_decision`` /
    ``cover_review`` / ``move_conflict`` is set (IMPORT-UI-7, FILES-CLEANUP-14,
    COVERS-SEARCH-21, LIBRARY-MOVE-5a)."""

    kind: Literal["changeset", "gate", "import", "cleanup", "cover", "move", "prune"]
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
    cleanup_decision: CleanupDecision | None = None
    cover_review: CoverReview | None = None
    move_conflict: MoveConflict | None = None
    prune_decision: PruneDecision | None = None
    busy: bool = Field(
        default=False,
        description="A decision job holds it; it isn't waiting any more (REVIEW-QUEUE-19)",
    )


class BeforeRun(_Model):
    """Values of an album's fields before its run changed them (LIBRARY-MOVE-18)."""

    album_id: str | None = None
    album: dict[str, FieldValue] = Field(
        default_factory=dict[str, FieldValue],
        description="Album fields this run changed, with their value before the run",
    )
    items: dict[str, dict[str, FieldValue]] = Field(
        default_factory=dict[str, dict[str, FieldValue]],
        description="Per item id: item fields this run changed, with their value before it",
    )
    folder: list[FolderItem] = Field(
        default_factory=list[FolderItem],
        description="The files' tags before the import, when the run decided the import",
    )


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


class CleanupDecisionBody(_Model):
    """Answer a cleanup decision (FILES-CLEANUP-14): ``delete`` what was listed for
    deletion, or ``keep`` everything."""

    decision: CleanupChoice
    note: str | None = Field(default=None, max_length=2000)


class PruneDecisionBody(_Model):
    """Answer a prune decision (LIBRARY-PRUNE-3): ``accept`` drops the listed albums'
    library entries, ``reject`` keeps them all."""

    decision: PruneChoice
    note: str | None = Field(default=None, max_length=2000)


class PruneDecided(_Model):
    """``job_id`` is the worker job that drops the albums (``accept``); ``None`` for
    ``reject``."""

    job_id: str | None = None


class InboxImportAllBody(_Model):
    """Start every folder of this workflow's watched inbox now (DASHBOARD-19)."""

    workflow: str = Field(max_length=200)


class InboxSkipped(_Model):
    input: str
    reason: str = Field(
        description="in_progress (a run is going on it or a folder around it), empty, "
        "outside (a link out of the inbox) or not_started (the workflow can't run now)"
    )


class InboxImportAll(_Model):
    started: list[str] = Field(description="Run ids, one per folder")
    skipped: list[InboxSkipped]


class CleanupDecided(_Model):
    """``job_id`` is the worker job that cleans the folder (``delete``); ``None`` for
    ``keep``, which needs no job."""

    job_id: str | None = None


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
    albums: list[str] | None = Field(
        default=None,
        min_length=1,
        description="Decide only these albums' lanes; the others keep waiting (DASHBOARD-11)",
    )


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


class WebhookSecretRequest(_Model):
    """Optional body of the secret route (SEC-87)."""

    replace: bool = False
    """Must be true to replace an existing secret (the old one stops working)."""


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


# -- dashboard (F-DASHBOARD) ---------------------------------------------------------

InboxStateOut = Literal[
    "settling",
    "waiting",
    "running",
    "waiting_review",
    "imported",
    "failed",
    "changed",
    "undone",
    "outside",
]


class InboxEntry(_Model):
    path: str = Field(description="Relative to the watched folder")
    state: InboxStateOut
    changed_at: datetime | None = Field(description="Last change, while settling")
    run_id: str | None = Field(description="The input's last run")


class InboxFolder(_Model):
    """One watch trigger's folder and its inputs (DASHBOARD-1)."""

    workflow: str
    folder: str
    trigger_state: Literal["armed", "disabled", "error"]
    detail: str | None
    entries: list[InboxEntry]
    omitted: int = Field(description="Inputs left out beyond the listing cap (DASHBOARD-2)")


class ImportedAlbum(_Model):
    id: str
    label: str
    items: int
    undoable: bool
    reason: str | None = Field(description="Why it can't be undone now")
    deletes_files: bool = Field(description="Whether undo deletes its files (DASHBOARD-18)")
    files_at: str | None = Field(description="The album's folder")


class RecentImport(_Model):
    """A run that imported albums still in the library (DASHBOARD-6)."""

    run_id: str
    workflow: str | None
    input: str | None
    state: JobState
    finished_at: datetime | None
    albums: list[ImportedAlbum]


class ImportUndoBody(_Model):
    albums: list[str] | None = Field(
        default=None, min_length=1, description="Album ids; all of the run's when absent"
    )


# -- cover search (S-COVERS-SEARCH) ------------------------------------------------------


class CoverSearchBody(_Model):
    """Start a cover search (COVERS-SEARCH-19). ``query.folder`` is ignored: the
    ``local`` provider only looks in the folder of the album under ``review_id``."""

    profile: str
    query: CoverQuery
    providers: list[str] | None = Field(default=None, description="Default: the profile's")
    refresh: bool = Field(default=False, description="Ignore cached results")
    review_id: str | None = Field(default=None, description="The cover review it is for")
    album_id: str | None = Field(
        default=None,
        description="The library album it is for, when there is no review (COVER-PICKER-14)",
    )


class CoverSearchStarted(_Model):
    search_id: str


class CoverDecisionBody(_Model):
    """Answer a cover review (COVERS-SEARCH-21). ``choose`` needs ``candidate_id``: one
    of the review's candidates, or one from a search started for it."""

    decision: CoverChoice
    candidate_id: str | None = None


class CoverQueryAnswer(_Model):
    """An album's cover query from the default templates (COVER-PICKER-2). ``query.folder``
    is left out: the server looks it up itself."""

    query: CoverQuery
    label: str
    art_sha256: str | None = None


class AlbumCoverBody(_Model):
    """Set an album's cover outside a run (COVER-PICKER-14). ``old_sha256`` is the art the
    user saw; a different current one comes back as a conflict."""

    candidate_id: str
    old_sha256: str | None = None


class AlbumCoverQueued(_Model):
    changeset_id: str
    job_id: str
    sha256: str
    queued_behind: list[str] = Field(default_factory=list[str])


class CoverUploadBody(_Model):
    """An image the user added, base64-encoded (COVER-PICKER-17). The profile is the
    ``profile`` query parameter, so the size cap is known before the body is read."""

    data: str = Field(description="The image file, base64")


class CoverUploaded(_Model):
    candidate: RankedCandidate


class CoverProviderOrder(_Model):
    name: str
    enabled: bool
    weight: float = Field(ge=0, le=10)


class CoverProvidersView(_Model):
    """The profile's cover providers as the picker edits them (COVER-PICKER-9, -10)."""

    providers: list[CoverProviderOrder] = Field(description="In the profile's order")
    registered: list[str] = Field(description="Every installed cover provider")
    profile_hash: str = Field(description="sha256 of profile.yaml as read")
    search_links: list[SearchLink] = Field(
        default_factory=list[SearchLink],
        description="Manual search pages, in order (COVER-PICKER-20)",
    )


class CoverProvidersBody(_Model):
    providers: list[CoverProviderOrder] = Field(description="In the new order")
    profile_hash: str


class MoveDecisionBody(_Model):
    """Answer a move conflict (LIBRARY-MOVE-5a): plan it again, or fail its lane."""

    decision: MoveChoice


class MoveDecided(_Model):
    """``job_id`` is the worker job that retries the move (``retry``); ``None`` otherwise."""

    job_id: str | None = None


class CoverDecided(_Model):
    """``job_id`` is the worker job that sets the cover (``choose``); ``None`` otherwise."""

    job_id: str | None = None
    sha256: str | None = None
