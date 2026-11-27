"""The public Step API: step types, context and results (RUN-3, RUN-4)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetProposal,
    ProposedFieldChange,
    ShowFields,
    Stamp,
    TargetRef,
)
from rifferr.core.choices import (
    ChoiceAI,
    ChoiceOffer,
    ChoiceOnReject,
    ChoiceSuggestInput,
    CustomField,
    LearnSettings,
)
from rifferr.core.covers import CoverQuery, RankedCandidate
from rifferr.core.files import BusyFolder, CleanupChoice, CleanupEntry, CleanupOptions
from rifferr.core.imports import ImportQuestion, TimeoutAction
from rifferr.core.moves import MovePlan, MoveSettings
from rifferr.core.notify import Notification, SendResult
from rifferr.core.profile import ProfileFile, Secret
from rifferr.core.prune import PruneAlbum
from rifferr.core.review import GateDecision, GateRequest, OnReject
from rifferr.core.selection import Scope, Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue
from rifferr.core.workflow import StepSpec, StepTypeInfo
from rifferr.engine.changesets import SnapshotIndex
from rifferr.engine.events import ENGINE_EVENTS
from rifferr.engine.prune import InMemoryRemovals, RemovalJournal
from rifferr.engine.run import FailedTarget, Scalar


class StepFailure(Exception):
    """Raise from a step to fail it with a message for the user."""


class StepRefused(StepFailure):
    """The step refuses to run at all (e.g. untrusted workflow). Always stops the run,
    whatever ``on_error`` says."""


class SecretAccess(Protocol):
    """Named profile secrets, resolved only when a step asks (PROFILE-5)."""

    def names(self) -> list[str]: ...

    def resolve(self, name: str) -> Secret:
        """Raise ``LookupError`` if unknown or unavailable; never include the value."""
        ...

    def env_refs(self) -> Mapping[str, str]:
        """Secret name -> environment variable, for secrets held in the environment
        (``env:`` references)."""
        ...


class NoSecrets:
    def names(self) -> list[str]:
        return []

    def resolve(self, name: str) -> Secret:
        raise LookupError(f"no secret named '{name}' in this profile")

    def env_refs(self) -> Mapping[str, str]:
        return {}


@dataclass(frozen=True)
class StepResult:
    outputs: dict[str, Scalar] = field(default_factory=dict[str, Scalar])


@runtime_checkable
class StepType(Protocol):
    """A step type plugin. Register it under the ``rifferr.steps`` entry-point group.

    Optional attribute ``force_review: bool`` (default False): the step's ChangeSets
    always wait for review, and ``apply: auto`` is a validation error (CHANGESET-6).

    Optional attribute ``source_kind`` (``"step"`` or ``"ai"``, default ``"step"``): the
    ``source.kind`` of the step's ChangeSets; ``"ai"`` for steps whose proposals come from
    a model (CHANGESET-1, S-AI-TAG AI-TAG-1).

    Optional attribute ``default_apply`` (``"auto"`` or ``"review"``, default ``"review"``):
    the ``apply`` policy when the workflow doesn't set one (CHANGESET-6).

    Optional attribute ``accepts_when: bool`` (default True): False for steps that
    aren't about albums; ``when:`` is then a validation error.

    Optional attribute ``run_level``: ``True``, or a function of the step's raw ``with:``
    mapping returning a bool (default False). A run-level step runs once per pass even
    before any lane exists, e.g. an import or a ``per: run`` command (C-LANES LANES-6).

    Optional attribute ``waits`` (``"apply"`` or ``"always"``, default: never): whether the
    step can put something in the review queue. ``"apply"``: when its ``apply`` is
    ``review`` (or it has ``force_review``). ``"always"``: whatever ``apply`` says, e.g. an
    import decision or a move conflict. The review page gives such steps a tab
    (UI-REFRESH-13, ``may_wait``).
    """

    name: str
    params_model: type[BaseModel]
    requires: BackendCapabilities

    async def run(self, ctx: StepContext) -> StepResult: ...


Emit = Callable[[str, dict[str, JsonValue]], None]
#: The engine's side of ``ctx.propose``: proposal, current target -> the ChangeSets made.
Propose = Callable[[ChangeSetProposal, TargetRef | None, SnapshotIndex], list[ChangeSet]]


#: The engine's side of ``ctx.propose_files``: one album's plan, optional stamp -> ChangeSet.
@dataclass(frozen=True)
class FileExtras:
    """What a FileChangeSet carries besides its files (S-COVERS-RESIZE COVERS-RESIZE-1):
    field changes to the plan's album, applied once every file is done, and the review's
    rationale (default: the folders)."""

    changes: tuple[ProposedFieldChange, ...] = ()
    rationale: str | None = None
    replan: MoveSettings | None = None
    """The settings to plan this move again with if the album changes while it waits in
    review (S-LIBRARY-MOVE LIBRARY-MOVE-19)."""


ProposeFiles = Callable[
    [MovePlan, Stamp | None, SnapshotIndex, ShowFields | None, FileExtras], ChangeSet
]


@dataclass(frozen=True)
class GateOptions:
    """How a review gate is decided besides by a person (S-REVIEW)."""

    on_reject: OnReject = "stop"
    timeout: timedelta | None = None
    on_timeout: GateDecision | None = None
    edit: bool = False
    """Values may be edited at the gate, held until continue (REVIEW-13 .. -15)."""


#: The engine's side of ``ctx.open_gate``: requests, options, snapshots -> gate id.
OpenGate = Callable[[list[GateRequest], GateOptions, SnapshotIndex], str]


@dataclass(frozen=True)
class ImportOptions:
    """How an import decision is applied and decided besides by a person (F-IMPORT-UI)."""

    options: Mapping[str, str | bool] = field(default_factory=dict[str, str | bool])
    """The step's importer options, used again when the decision is applied."""
    set_fields: Mapping[str, str] = field(default_factory=dict[str, str])
    timeout: timedelta | None = None
    on_timeout: TimeoutAction = "skip"


class NotifyTargets(Protocol):
    """A profile's notification targets (S-NOTIFY): the worker provides them."""

    @property
    def base_url(self) -> str | None: ...

    def names(self) -> list[str]: ...

    async def send(self, target: str, n: Notification) -> SendResult:
        """Send with the profile's retries; never raises for a failed send."""
        ...


#: The engine's side of ``ctx.notify``: targets (None = all), notification -> results.
SendNotification = Callable[[list[str] | None, Notification], Awaitable[list[SendResult]]]


@dataclass(frozen=True)
class CleanupRequest:
    """A listing to leave in the review queue before deleting (FILES-CLEANUP-14)."""

    path: str
    entries: list[CleanupEntry]
    totals: Mapping[str, int]
    options: CleanupOptions
    truncated: int = 0
    timeout: timedelta | None = None
    on_timeout: CleanupChoice = "keep"


@dataclass(frozen=True)
class PruneRequest:
    """Albums whose files are gone, to leave in the review queue (LIBRARY-PRUNE-3)."""

    albums: list[PruneAlbum]
    partial: list[PruneAlbum]


@dataclass(frozen=True)
class CoverRequest:
    """An album waiting for a person to pick its cover (COVERS-SEARCH-14)."""

    album_id: str
    label: str
    query: CoverQuery
    candidates: list[RankedCandidate]
    dropped: Mapping[str, Mapping[str, int]] = field(default_factory=dict[str, Mapping[str, int]])
    old_sha256: str | None = None
    reason: str = ""


#: The engine's side of ``ctx.request_cover``: request -> review id.
RequestCover = Callable[[CoverRequest], str]


@dataclass(frozen=True)
class ChoiceRequest:
    """An album waiting for a person to choose an outcome (S-TAGS-CHOOSE TAGS-CHOOSE-3)."""

    album_id: str
    label: str
    options: list[ChoiceOffer]
    message: str | None = None
    shown: Mapping[str, FieldValue] = field(default_factory=dict[str, FieldValue])
    suggested: str | None = None
    custom: list[CustomField] = field(default_factory=list[CustomField])
    known_name: str | None = None
    known_name_value: str | None = None
    known_name_option: str | None = None
    known_name_actions: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    learn: LearnSettings | None = None
    on_reject: ChoiceOnReject = "stop"
    ai_input: ChoiceSuggestInput | None = None  # F-AI-SUGGEST AI-SUGGEST-2
    ai: ChoiceAI | None = None  # suggestions asked when the review opened (AI-SUGGEST-8)


#: The engine's side of ``ctx.request_choice``: request -> review id.
RequestChoice = Callable[[ChoiceRequest], str]


class AIAnswer(BaseModel):
    """A validated AI task result as steps see it (C-AI-CORE AI-CORE-9)."""

    value: JsonValue
    rejected: list[dict[str, JsonValue]] = []
    usage: dict[str, JsonValue] = {}
    provider: str
    model: str
    prompt_version: int
    cached: bool = False


class AIUnavailableError(Exception):
    """An AI task gave no answer: not configured, a bad file, or attempts ran out."""


AIEmit = Callable[[str, dict[str, JsonValue]], None]


class AIAccess(Protocol):
    """The worker's side of ``ctx.ask_ai``: runs an installed AI task (C-AI-CORE).
    Steps never see providers, keys or hosts."""

    @property
    def configured(self) -> bool: ...

    async def run_task(
        self,
        task: str,
        input: Mapping[str, JsonValue],
        *,
        provider: str | None,
        target: str,
        emit: AIEmit,
        retries: int = 2,
        refresh: bool = False,
    ) -> AIAnswer: ...


#: The engine's side of ``ctx.request_move_conflict``: plan, settings -> conflict id.
RequestMoveConflict = Callable[[MovePlan, MoveSettings], str]


#: The engine's side of ``ctx.request_cleanup``: request -> decision id.
RequestCleanup = Callable[[CleanupRequest], str]
#: The engine's side of ``ctx.request_prune``: request -> decision id.
RequestPrune = Callable[[PruneRequest], str]
#: The engine's side of ``ctx.hold_pass``: kind, ref, label for a pass without lanes.
HoldPass = Callable[[str, str, str], None]


#: The engine's side of ``ctx.request_import``: question, options -> decision id.
RequestImport = Callable[[ImportQuestion, ImportOptions], str]
#: The engine's side of ``ctx.hold``: key, kind, ref, label.
OnHold = Callable[[str, str, str, str | None], None]


def _no_propose(
    proposal: ChangeSetProposal, current: TargetRef | None, s: SnapshotIndex
) -> list[ChangeSet]:
    raise StepFailure("this run can't propose ChangeSets")


def _no_propose_files(
    plan: MovePlan,
    stamp: Stamp | None,
    s: SnapshotIndex,
    show: ShowFields | None,
    extras: FileExtras,
) -> ChangeSet:
    raise StepFailure("this step can't propose file moves here")


def _no_gate(requests: list[GateRequest], options: GateOptions, s: SnapshotIndex) -> str:
    raise StepFailure("this run can't open review gates")


def _no_import(question: ImportQuestion, options: ImportOptions) -> str:
    raise StepFailure("this run can't ask for import decisions")


def _no_imported(path: str) -> None:
    """Nothing else tracks import decisions (tests, runs without a worker)."""


async def _no_notify(targets: list[str] | None, n: Notification) -> list[SendResult]:
    raise StepFailure("this profile has no notification targets (notify.targets)")


def _no_cleanup(request: CleanupRequest) -> str:
    raise StepFailure("this run can't leave cleanup decisions")


def _no_prune(request: PruneRequest) -> str:
    raise StepFailure("this run can't leave prune decisions")


def _no_cover(request: CoverRequest) -> str:
    raise StepFailure("this run can't leave cover reviews")


def _no_choice(request: ChoiceRequest) -> str:
    raise StepFailure("this run can't leave choice reviews")


def _no_move_conflict(plan: MovePlan, settings: MoveSettings) -> str:
    raise StepFailure("this run can't leave move conflicts")


def _no_hold_pass(kind: str, ref: str, label: str) -> None:
    raise RuntimeError("this context can't hold passes")


def _unchanged(value: str) -> str:
    return value


class StepContext:
    """What a running step can see and do. Steps get nothing else (RUN-4)."""

    def __init__(
        self,
        *,
        run_id: str,
        step: StepSpec,
        params: BaseModel,
        backend: LibraryBackend,
        selection: Selection,
        emit: Emit,
        is_cancelled: Callable[[], bool],
        profile: str = "",
        vars: Mapping[str, str] | None = None,
        work_dir: Path | None = None,
        trusted: bool = False,
        secrets: SecretAccess | None = None,
        on_secret: Callable[[Secret], None] | None = None,
        redact: Callable[[str], str] | None = None,
        propose: Propose | None = None,
        propose_files: ProposeFiles | None = None,
        profile_dir: Path | None = None,
        interpolate: Callable[[str], str] | None = None,
        interpolate_template: Callable[[str], str] | None = None,
        open_gate: OpenGate | None = None,
        on_hold: OnHold | None = None,
        request_import: RequestImport | None = None,
        notify: SendNotification | None = None,
        notify_targets: Iterable[str] = (),
        base_url: str | None = None,
        profile_settings: ProfileFile | None = None,
        busy_folders: Callable[[], list[BusyFolder]] | None = None,
        watched_folders: Callable[[], list[Path]] | None = None,
        imported_folder: Callable[[str], None] | None = None,
        request_cleanup: RequestCleanup | None = None,
        hold_pass: HoldPass | None = None,
        request_cover: RequestCover | None = None,
        request_choice: RequestChoice | None = None,
        request_move_conflict: RequestMoveConflict | None = None,
        run_input: str = "",
        request_prune: RequestPrune | None = None,
        removals: RemovalJournal | None = None,
        ai: AIAccess | None = None,
        ai_emit: AIEmit | None = None,
        workflow: str = "",
    ) -> None:
        self.run_id = run_id
        self.workflow = workflow
        """The run's workflow name, e.g. for per-step settings (AI-SUGGEST-7)."""
        self._ai = ai
        self._ai_emit: AIEmit = ai_emit or (lambda type_, data: None)
        self.run_input = run_input
        """``{{ run.input }}``, for steps that pass it to a process in the environment."""
        self.profile = profile
        self.vars: Mapping[str, str] = dict(vars or {})
        self.work_dir = work_dir
        self.profile_dir = profile_dir
        """The profile's folder (``scripts/``, ``rules/`` ... live under it), if known."""
        self._interpolate: Callable[[str], str] = interpolate or _unchanged
        self._interpolate_template: Callable[[str], str] = (
            interpolate_template or interpolate or _unchanged
        )
        self.trusted = trusted
        """Whether this run's workflow is trusted to run shell/python steps (F-SECURITY)."""
        self._secrets = secrets or NoSecrets()
        self._on_secret = on_secret
        self._redact = redact or _unchanged
        self.step = step
        self.params = params
        self.backend = backend
        self.selection = selection
        self._emit = emit
        self._is_cancelled = is_cancelled
        self.failed_targets: list[FailedTarget] = []
        self._propose = propose or _no_propose
        self._propose_files = propose_files or _no_propose_files
        self._open_gate = open_gate or _no_gate
        self._on_hold = on_hold or _no_hold
        self._request_import = request_import or _no_import
        self._notify = notify or _no_notify
        self.notify_targets: list[str] = list(notify_targets)
        """The profile's notification target names (S-NOTIFY NOTIFY-1)."""
        self.base_url = base_url
        self.profile_settings = profile_settings
        """The run's profile settings (``profile.yaml``), when the worker knows them."""
        self._busy_folders: Callable[[], list[BusyFolder]] = busy_folders or list[BusyFolder]
        self._watched_folders: Callable[[], list[Path]] = watched_folders or list[Path]
        self._imported_folder: Callable[[str], None] = imported_folder or _no_imported
        self._request_cleanup = request_cleanup or _no_cleanup
        self._hold_pass = hold_pass or _no_hold_pass
        self._request_cover = request_cover or _no_cover
        self._request_choice = request_choice or _no_choice
        self._request_move_conflict = request_move_conflict or _no_move_conflict
        self._request_prune = request_prune or _no_prune
        self.removals: RemovalJournal = removals or InMemoryRemovals()
        """Where album removals are recorded before and after (LIBRARY-PRUNE-4)."""
        """The profile's ``notify.base_url``, for links in notifications (NOTIFY-6)."""
        self._snapshots = SnapshotIndex()
        self.proposed: list[ChangeSet] = []
        """Every ChangeSet this step proposed so far, in order (after no-op dropping)."""

    @property
    def cancelled(self) -> bool:
        """True once the run is being cancelled; long steps should check it and return."""
        return self._is_cancelled()

    @staticmethod
    def check_event_type(type: str) -> None:
        if type in ENGINE_EVENTS:
            raise ValueError(f"{type} is emitted by the engine, not by steps")
        if not type.startswith("run.step."):
            raise ValueError(f"step events must be named run.step.<name>, got {type!r}")

    def emit(self, type: str, **data: JsonValue) -> None:
        self.check_event_type(type)
        self._emit(type, data)

    def log(self, line: str, stream: str = "info", target: str | None = None) -> None:
        data: dict[str, JsonValue] = {"stream": stream, "line": line}
        if target is not None:
            data["target"] = target
        self._emit("run.step.log", data)

    def redact(self, text: str) -> str:
        """Mask this run's known secret values in ``text`` (for steps that cut text up)."""
        return self._redact(text)

    def interpolate(self, value: str) -> str:
        """``{{ ... }}`` interpolation (C-WORKFLOW) with this step's context, for text a
        step reads itself (e.g. a script file). In a step from a called workflow, ``vars``
        and ``steps`` mean that workflow's, as in its ``with:`` (SUBWORKFLOW-2, -4).
        Raises ``StepFailure`` on a bad expression."""
        return self._interpolate(value)

    def interpolate_template(self, value: str) -> str:
        """Like ``interpolate``, for text the backend then evaluates as a template:
        values from outside the workflow come out escaped, so they stay literal (SEC-22)."""
        return self._interpolate_template(value)

    def secret(self, name: str) -> Secret:
        """Resolve a profile secret by name. Its value is masked in this run's events."""
        try:
            secret = self._secrets.resolve(name)
        except LookupError as exc:
            raise StepFailure(f"secret '{name}' is not available: {exc}") from None
        if self._on_secret is not None:
            self._on_secret(secret)
        return secret

    def secret_env_refs(self) -> Mapping[str, str]:
        """Secret name -> inherited environment variable holding it (``env:`` secrets)."""
        return self._secrets.env_refs()

    def albums(self, scope: Scope = "album") -> list[AlbumSnapshot]:
        """This step's targets: the run's albums, narrowed by the step's ``when``."""
        albums = self.backend.query(self.selection.model_copy(update={"scope": scope}))
        self._snapshots.add(albums)
        return albums

    def propose(self, proposal: ChangeSetProposal, current: TargetRef | None = None) -> list[str]:
        """Propose changes to albums or items this step got from ``albums()`` (CHANGESET-3).

        ``current`` resolves the short form ``changes``. Missing ``old`` values come
        from those snapshots; no-op changes are dropped. One ChangeSet per album;
        returns their ids (empty if nothing changes). The step's ``apply`` policy then
        applies them right away (``auto``) or leaves them for review (``review``).
        """
        made = self._propose(proposal, current, self._snapshots)
        self.proposed.extend(made)
        return [cs.id for cs in made]

    def propose_files(
        self,
        plan: MovePlan,
        stamp: Stamp | None = None,
        show: ShowFields | None = None,
        extras: FileExtras | None = None,
    ) -> str:
        """Propose one album's file moves (S-LIBRARY-MOVE) as a FileChangeSet, accepted or
        rejected as a whole. ``plan.album`` must come from ``albums()``. ``extras`` adds
        field changes to that album, applied after the files, and a rationale. The step's
        ``apply`` policy applies it now (``auto``; a file failure raises ``StepFailure``)
        or leaves it for review. Returns its id."""
        made = self._propose_files(plan, stamp, self._snapshots, show, extras or FileExtras())
        self.proposed.append(made)
        return made.id

    def open_gate(self, requests: list[GateRequest], options: GateOptions | None = None) -> str:
        """Hold the run for a human decision on albums from ``albums()`` (RUN-14, S-REVIEW).

        The engine stores a review gate with each album's label, the run's changes to it
        so far and its conflicted ChangeSets, emits ``review.requested`` and, once this
        step finishes, suspends the run in ``waiting_review`` until the gate is decided.
        Returns the gate id.
        """
        if not requests:
            raise ValueError("a review gate needs at least one album")
        return self._open_gate(list(requests), options or GateOptions(), self._snapshots)

    def fail_target(self, target: str, message: str) -> None:
        """Record a failed target (an album or item id) and return, so the step goes on
        with its other targets. After the pass the engine applies the step's ``on_error``
        to that album's lane (C-LANES LANES-10)."""
        self.failed_targets.append(FailedTarget(target, message))
        self.log(f"{target}: {message}", stream="error")

    def hold(self, key: str, kind: str, ref: str, label: str | None = None) -> None:
        """Put lane ``key`` (an album id, or ``pending:<id>`` for an album not imported
        yet) on hold until ``ref`` is resolved (C-LANES LANES-14). ``label`` names a lane
        this creates (default: ``key``)."""
        self._on_hold(key, kind, ref, label)

    def request_import(self, question: ImportQuestion, options: ImportOptions | None = None) -> str:
        """Leave an album folder for an import decision (F-IMPORT-UI IMPORT-UI-4, -5).

        The engine stores the decision, emits ``import.requested`` and holds a new lane
        ``pending:<id>`` until the decision is applied. Returns the decision id.
        """
        return self._request_import(question, options or ImportOptions())

    def busy_folders(self) -> list[BusyFolder]:
        """Folders other work still needs: pending import decisions of this profile and
        the inputs of its other queued, running or waiting runs (FILES-CLEANUP-7)."""
        return self._busy_folders()

    def watched_folders(self) -> list[Path]:
        """The profile's watch folders (``on.watch.path`` of its workflows): steps that
        remove folders must leave these in place (FILES-CLEANUP-18)."""
        return self._watched_folders()

    def imported_folder(self, path: str) -> None:
        """Tell the engine this step imported the album folder ``path``, so other runs'
        pending import decisions for that folder are superseded (IMPORT-UI-19)."""
        self._imported_folder(path)

    def hold_pass(self, kind: str, ref: str, label: str) -> None:
        """Hold every lane of this pass on ``ref`` (C-LANES LANES-16, run-level steps). A
        pass without lanes waits in a new lane named by ``label``."""
        self._hold_pass(kind, ref, label)

    def request_cleanup(self, request: CleanupRequest) -> str:
        """Leave a cleanup listing in the review queue and hold the pass until it is
        decided (FILES-CLEANUP-14). Returns the decision id."""
        return self._request_cleanup(request)

    def request_prune(self, request: PruneRequest) -> str:
        """Leave the albums to drop in the review queue and hold the pass until it is
        decided (LIBRARY-PRUNE-3). Returns the decision id."""
        return self._request_prune(request)

    def request_cover(self, request: CoverRequest) -> str:
        """Leave an album in the review queue to pick its cover; only that album's lane
        waits (COVERS-SEARCH-14). Returns the review id."""
        return self._request_cover(request)

    def request_choice(self, request: ChoiceRequest) -> str:
        """Leave an album in the review queue to choose one of ``request.options`` or type
        values; only that album's lane waits (S-TAGS-CHOOSE TAGS-CHOOSE-3). The album must
        come from ``albums()``. The decision becomes one ChangeSet applied through the
        engine. Returns the review id."""
        if self._snapshots.album(request.album_id) is None:
            raise StepFailure(f"album {request.album_id} is not one of this step's targets")
        return self._request_choice(request)

    @property
    def ai_configured(self) -> bool:
        """Whether ``ask_ai`` can run: the profile has an enabled AI provider."""
        return self._ai is not None and self._ai.configured

    async def ask_ai(
        self,
        task: str,
        input: BaseModel,
        *,
        provider: str | None = None,
        target: str = "",
        retries: int = 2,
        refresh: bool = False,
    ) -> AIAnswer:
        """Run the installed AI task ``task`` on ``input`` (C-AI-CORE AI-CORE-9). Its
        ``ai.*`` events go on this run. Raises ``AIUnavailableError`` with a reason when
        there is no answer; the step decides what that means. The answer is validated,
        never applied: what to propose is the step's job (non-negotiable 4)."""
        if self._ai is None or not self._ai.configured:
            raise AIUnavailableError("AI is not configured for this profile")
        return await self._ai.run_task(
            task,
            input.model_dump(mode="json"),
            provider=provider,
            target=target,
            emit=self._ai_emit,
            retries=retries,
            refresh=refresh,
        )

    def ai_rejected(self, value: str, reason: str, *, field: str | None, target: str) -> None:
        """Report a value of an AI answer that the step dropped after the task's own
        checks, e.g. one the backend can't store (``ai.rejected``, AI-CORE-15)."""
        self._ai_emit(
            "ai.rejected",
            {"value": value[:200], "reason": reason, "field": field, "target": target},
        )

    def request_move_conflict(self, plan: MovePlan, settings: MoveSettings) -> str:
        """Leave a conflicted album in the review queue; only its lane waits until it is
        retried or failed (S-LIBRARY-MOVE LIBRARY-MOVE-5a). Returns the conflict id."""
        return self._request_move_conflict(plan, settings)

    async def notify(
        self, notification: Notification, targets: list[str] | None = None
    ) -> list[SendResult]:
        """Send to the profile's targets (``None``: all) with its retries (S-NOTIFY).
        The engine emits ``notify.sent`` / ``notify.failed`` per target; a failed send
        is a result, not an exception."""
        return await self._notify(targets, notification)


def _no_hold(key: str, kind: str, ref: str, label: str | None) -> None:
    raise RuntimeError("this context can't hold lanes")


def is_run_level(step_type: object, with_: Mapping[str, Any]) -> bool:
    """Whether a step runs before any lane exists (``run_level``, LANES-6)."""
    flag = getattr(step_type, "run_level", False)
    if callable(flag):
        return bool(flag(with_))
    return bool(flag)


_REQUIRED = ("name", "params_model", "requires", "run")


class StepTypeRegistry:
    """Step types by name. Also serves workflow validation (``core.workflow.StepRegistry``)."""

    def __init__(self, types: Iterable[Any], errors: Iterable[str] = ()) -> None:
        self._types: dict[str, StepType] = {}
        self.errors = list(errors)
        for t in types:
            missing = [a for a in _REQUIRED if not hasattr(t, a)]
            if missing:
                raise TypeError(f"{t!r} is not a step type: missing {', '.join(missing)}")
            step: StepType = t
            if step.name in self._types:
                raise ValueError(f"step type {step.name} registered twice")
            self._types[step.name] = step

    def step_type(self, name: str) -> StepType | None:
        return self._types.get(name)

    def get(self, name: str) -> StepTypeInfo | None:
        t = self._types.get(name)
        if t is None:
            return None
        return StepTypeInfo(
            name=t.name,
            params_model=t.params_model,
            requires=t.requires,
            force_review=bool(getattr(t, "force_review", False)),
            accepts_when=bool(getattr(t, "accepts_when", True)),
        )

    def names(self) -> list[str]:
        return sorted(self._types)
