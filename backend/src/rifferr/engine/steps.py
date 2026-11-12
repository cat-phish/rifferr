"""The public Step API: step types, context and results (RUN-3, RUN-4)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, JsonValue

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.changeset import ChangeSet, ChangeSetProposal, TargetRef
from rifferr.core.profile import Secret
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
    """

    name: str
    params_model: type[BaseModel]
    requires: BackendCapabilities

    async def run(self, ctx: StepContext) -> StepResult: ...


Emit = Callable[[str, dict[str, JsonValue]], None]
#: The engine's side of ``ctx.propose``: proposal, current target -> the ChangeSets made.
Propose = Callable[[ChangeSetProposal, TargetRef | None, SnapshotIndex], list[ChangeSet]]


def _no_propose(
    proposal: ChangeSetProposal, current: TargetRef | None, s: SnapshotIndex
) -> list[ChangeSet]:
    raise StepFailure("this run can't propose ChangeSets")


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
    ) -> None:
        self.run_id = run_id
        self.profile = profile
        self.vars: Mapping[str, str] = dict(vars or {})
        self.work_dir = work_dir
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

    def fail_target(self, target: str, message: str) -> None:
        """Record a failed target. Returns under ``on_error: continue``; otherwise ends
        the step (RUN-5)."""
        self.failed_targets.append(FailedTarget(target, message))
        self.log(f"{target}: {message}", stream="error")
        if self.step.on_error != "continue":
            raise StepFailure(message)


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
