"""The public Step API: step types, context and results (RUN-3, RUN-4)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.changeset import ChangeSet, ChangeSetProposal, TargetRef
from rifferr.core.imports import ImportQuestion, TimeoutAction
from rifferr.core.profile import Secret
from rifferr.core.review import GateDecision, GateRequest, OnReject
from rifferr.core.selection import Scope, Selection
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.core.workflow import StepSpec, StepTypeInfo
from rifferr.engine.changesets import SnapshotIndex
from rifferr.engine.events import ENGINE_EVENTS
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

    Optional attribute ``run_level``: ``True``, or a function of the step's raw ``with:``
    mapping returning a bool (default False). A run-level step runs once per pass even
    before any lane exists, e.g. an import or a ``per: run`` command (C-LANES LANES-6).
    """

    name: str
    params_model: type[BaseModel]
    requires: BackendCapabilities

    async def run(self, ctx: StepContext) -> StepResult: ...


Emit = Callable[[str, dict[str, JsonValue]], None]
#: The engine's side of ``ctx.propose``: proposal, current target -> the ChangeSets made.
Propose = Callable[[ChangeSetProposal, TargetRef | None, SnapshotIndex], list[ChangeSet]]


@dataclass(frozen=True)
class GateOptions:
    """How a review gate is decided besides by a person (S-REVIEW)."""

    on_reject: OnReject = "stop"
    timeout: timedelta | None = None
    on_timeout: GateDecision | None = None


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


#: The engine's side of ``ctx.request_import``: question, options -> decision id.
RequestImport = Callable[[ImportQuestion, ImportOptions], str]
#: The engine's side of ``ctx.hold``: key, kind, ref, label.
OnHold = Callable[[str, str, str, str | None], None]


def _no_propose(
    proposal: ChangeSetProposal, current: TargetRef | None, s: SnapshotIndex
) -> list[ChangeSet]:
    raise StepFailure("this run can't propose ChangeSets")


def _no_gate(requests: list[GateRequest], options: GateOptions, s: SnapshotIndex) -> str:
    raise StepFailure("this run can't open review gates")


def _no_import(question: ImportQuestion, options: ImportOptions) -> str:
    raise StepFailure("this run can't ask for import decisions")


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
        propose: Propose | None = None,
        profile_dir: Path | None = None,
        interpolate: Callable[[str], str] | None = None,
        open_gate: OpenGate | None = None,
        on_hold: OnHold | None = None,
        request_import: RequestImport | None = None,
    ) -> None:
        self.run_id = run_id
        self.profile = profile
        self.vars: Mapping[str, str] = dict(vars or {})
        self.work_dir = work_dir
        self.profile_dir = profile_dir
        """The profile's folder (``scripts/``, ``rules/`` ... live under it), if known."""
        self._interpolate: Callable[[str], str] = interpolate or _unchanged
        self.trusted = trusted
        """Whether this run's workflow is trusted to run shell/python steps (F-SECURITY)."""
        self._secrets = secrets or NoSecrets()
        self._on_secret = on_secret
        self.step = step
        self.params = params
        self.backend = backend
        self.selection = selection
        self._emit = emit
        self._is_cancelled = is_cancelled
        self.failed_targets: list[FailedTarget] = []
        self._propose = propose or _no_propose
        self._open_gate = open_gate or _no_gate
        self._on_hold = on_hold or _no_hold
        self._request_import = request_import or _no_import
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

    def interpolate(self, value: str) -> str:
        """``{{ ... }}`` interpolation (C-WORKFLOW) with this step's context, for text a
        step reads itself (e.g. a script file). Raises ``StepFailure`` on a bad expression."""
        return self._interpolate(value)

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
        )

    def names(self) -> list[str]:
        return sorted(self._types)
