"""The ``shell`` step (S-SHELL): run a command once per run, album or item.

With ``emits: none`` (default) stdout and stderr stream into the run log. With
``emits: changes`` stdout is collected and parsed as one ChangeSetProposal,
which goes through ``ctx.propose`` (SHELL-14 .. -19). Commands run in their own
process group so a timeout or cancellation can stop everything they started
(POSIX only).
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from rifferr.backends.base import BackendCapabilities, CapabilityError, TemplateError
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetProposal,
    ChangeSource,
    FieldChange,
    TargetChange,
    TargetRef,
)
from rifferr.core.profile import env_var_name
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot
from rifferr.engine import StepContext, StepFailure, StepRefused, StepResult
from rifferr.steps.process import run_process

SECRET_ENV_PREFIX = "RIFFERR_SECRET_"
MAX_PROPOSAL = 16 * 1024 * 1024
"""Most stdout collected with ``emits: changes`` (SHELL-14)."""

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ShellParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: list[str] | str = Field(
        description="Command as an argv list, or a string run by /bin/sh -c (needs shell: true)"
    )
    shell: bool = Field(default=False, description="Run a string command through /bin/sh -c")
    per: Literal["run", "album", "item"] = "album"
    timeout: PositiveInt = Field(default=600, description="Seconds before the command is stopped")
    cwd: str | None = Field(default=None, description="Working directory (templated)")
    env: dict[str, str] = Field(default_factory=dict[str, str], description="Extra variables")
    secrets: list[str] = Field(
        default_factory=list[str],
        description="Profile secrets to expose as RIFFERR_SECRET_<NAME>",
    )
    emits: Literal["none", "changes"] = Field(
        default="none",
        description="none: stdout goes to the run log. changes: stdout is one JSON "
        "ChangeSetProposal (schemas/changeset-proposal.schema.json).",
    )

    @field_validator("run")
    @classmethod
    def _non_empty(cls, value: list[str] | str) -> list[str] | str:
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("env")
    @classmethod
    def _env_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [k for k in value if not _ENV_NAME.match(k)]
        if bad:
            raise ValueError(f"not a valid environment variable name: {', '.join(bad)}")
        return value

    @model_validator(mode="after")
    def _shell_form(self) -> ShellParams:
        if isinstance(self.run, str) and not self.shell:
            raise ValueError(
                "run: a single string needs 'shell: true'; otherwise give an argv list "
                "like [ls, -la, $path]"
            )
        if isinstance(self.run, list) and self.shell:
            raise ValueError("run: 'shell: true' needs a single command string, not a list")
        return self


@dataclass(frozen=True)
class _Target:
    id: str | None
    snapshot: AlbumSnapshot | ItemSnapshot | None
    stdin: bytes
    album_dir: str | None
    item_path: str | None
    scope: frozenset[tuple[str, str]] = frozenset()
    """``(scope, id)`` of what this invocation may change (SHELL-16)."""
    current: TargetRef | None = None
    """What the short form ``changes`` targets (SHELL-15)."""

    @property
    def label(self) -> str:
        return self.id or "run"


class _Collector:
    """stdout of an ``emits: changes`` invocation, up to MAX_PROPOSAL bytes."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.overflow = False

    def add(self, chunk: bytes) -> None:
        if self.overflow:
            return
        if len(self.data) + len(chunk) > MAX_PROPOSAL:
            self.overflow = True
            self.data.clear()
        else:
            self.data += chunk


class ShellStep:
    name = "shell"
    params_model = ShellParams
    requires = BackendCapabilities(query=True, templates=True)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    @staticmethod
    def run_level(with_: Mapping[str, Any]) -> bool:
        """``per: run`` runs once per pass, with or without albums (LANES-6)."""
        return with_.get("per") == "run"

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, ShellParams)
        if not ctx.trusted:
            raise StepRefused(
                "this workflow is not trusted, so shell steps can't run; "
                "trust it before running it (F-SECURITY)"
            )
        if sys.platform == "win32":
            raise StepRefused("shell steps need Linux or macOS; on Windows use the Docker image")
        base_env = self._base_env(ctx, params)
        for target in _targets(ctx, params):
            if ctx.cancelled:
                break
            try:
                argv, cwd, extra = _render(ctx, params, target)
            except TemplateError as exc:
                _fail(ctx, target, f"template error: {exc}")
                continue
            env = {**base_env, **extra, **_fixed_env(ctx, target)}
            if cwd is None:
                cwd = target.album_dir or (str(ctx.work_dir) if ctx.work_dir else None)
            collect = _Collector() if params.emits == "changes" else None
            problem = await run_process(
                ctx,
                argv,
                cwd=cwd,
                env=env,
                timeout=params.timeout,
                stdin=target.stdin,
                target=target.id,
                stdout_sink=collect.add if collect is not None else None,
            )
            if ctx.cancelled:
                continue
            if problem is None and collect is not None:
                problem = _propose(ctx, params, target, collect)
            if problem is not None:
                _fail(ctx, target, problem)
        return StepResult()

    def _base_env(self, ctx: StepContext, params: ShellParams) -> dict[str, str]:
        environ = os.environ if self._environ is None else self._environ
        env = inherited_env(ctx, environ, keep=params.secrets)
        if ctx.backend.capabilities.command_env:
            env.update(ctx.backend.command_env())
        for name, value in ctx.vars.items():
            env[env_var_name(name)] = value
        for name in params.secrets:
            env[secret_env_name(name)] = ctx.secret(name).reveal()
        return env


def inherited_env(
    ctx: StepContext, environ: Mapping[str, str], keep: list[str] | None = None
) -> dict[str, str]:
    """Rifferr's environment without variables holding profile ``env:`` secrets, except
    the secrets named in ``keep`` (SHELL-4)."""
    listed = set(keep or [])
    hidden = {var for name, var in ctx.secret_env_refs().items() if name not in listed}
    return {k: v for k, v in environ.items() if k not in hidden}


def secret_env_name(name: str) -> str:
    """``discogs_token`` -> ``RIFFERR_SECRET_DISCOGS_TOKEN``."""
    return SECRET_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


# -- targets ------------------------------------------------------------------


def _album_scope(album: AlbumSnapshot) -> set[tuple[str, str]]:
    return {("album", album.id)} | {("item", i.id) for i in album.items}


def _targets(ctx: StepContext, params: ShellParams) -> list[_Target]:
    if params.per == "run":
        albums = ctx.albums()
        data = {"albums": [a.model_dump(mode="json") for a in albums]}
        scope = frozenset(r for a in albums for r in _album_scope(a))
        return [_Target(None, None, json.dumps(data).encode(), None, None, scope)]
    if params.per == "album":
        return [
            _Target(
                a.id,
                a,
                a.model_dump_json().encode(),
                a.path,
                None,
                frozenset(_album_scope(a)),
                TargetRef(scope="album", id=a.id),
            )
            for a in ctx.albums()
        ]
    out: list[_Target] = []
    for a in ctx.albums("item"):
        for i in a.items:
            ref = TargetRef(scope="item", id=i.id)
            out.append(
                _Target(
                    i.id,
                    i,
                    i.model_dump_json().encode(),
                    a.path,
                    i.path,
                    frozenset({("item", i.id)}),
                    ref,
                )
            )
    return out


def _render(
    ctx: StepContext, params: ShellParams, target: _Target
) -> tuple[list[str], str | None, dict[str, str]]:
    """Evaluate beets templates per target; never split words (SHELL-2)."""
    snapshot = target.snapshot

    def ev(value: str) -> str:
        return value if snapshot is None else ctx.backend.evaluate(value, snapshot)

    if isinstance(params.run, str):
        # The shell string is not template-evaluated: tag values would become shell
        # code. Scripts read RIFFERR_ALBUM_DIR / RIFFERR_ITEM_PATH or stdin instead.
        argv = ["/bin/sh", "-c", params.run]
    else:
        argv = [ev(a) for a in params.run]
    cwd = ev(params.cwd) if params.cwd is not None else None
    return argv, cwd, {k: ev(v) for k, v in params.env.items()}


def _fixed_env(ctx: StepContext, target: _Target) -> dict[str, str]:
    env = {"RIFFERR_RUN_ID": ctx.run_id, "RIFFERR_PROFILE": ctx.profile}
    if target.album_dir is not None:
        env["RIFFERR_ALBUM_DIR"] = target.album_dir
    if target.item_path is not None:
        env["RIFFERR_ITEM_PATH"] = target.item_path
    return env


def _fail(ctx: StepContext, target: _Target, message: str) -> None:
    if target.id is None:
        raise StepFailure(message)
    ctx.fail_target(target.id, message)


# -- proposals (SHELL-14 .. -19) ------------------------------------------------


def _propose(
    ctx: StepContext, params: ShellParams, target: _Target, collect: _Collector
) -> str | None:
    """Turn collected stdout into ChangeSets; return a failure message, or None."""
    if collect.overflow:
        return f"stdout is larger than {MAX_PROPOSAL // (1024 * 1024)} MiB"
    text = collect.data.decode(errors="replace")
    if not text.strip():
        ctx.log(f"{target.label}: no changes", target=target.id)
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"stdout is not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"
    try:
        proposal = ChangeSetProposal.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        return f"invalid ChangeSet proposal at {_json_path(first['loc'])}: {first['msg']}"
    if proposal.changes is not None and target.current is None:
        return "invalid ChangeSet proposal at $: per: run needs 'targets', not 'changes'"
    for i, t in enumerate(proposal.targets or []):
        if (t.target.scope, t.target.id) not in target.scope:
            return (
                f"invalid ChangeSet proposal at $.targets[{i}].target: "
                f"{t.target.label()} is outside this invocation ({_scope_name(params.per)})"
            )
    problem = _check(ctx, proposal, target)
    if problem is not None:
        return problem
    before = len(ctx.proposed)
    try:
        ctx.propose(proposal, current=target.current)
    except StepFailure as exc:
        return str(exc)
    made = ctx.proposed[before:]
    if not made:
        ctx.log(f"{target.label}: no changes", target=target.id)
    else:
        fields = sum(len(cs.field_changes()) for cs in made)
        ctx.log(
            f"{target.label}: proposed {_count(len(made), 'ChangeSet')}, "
            f"{_count(fields, 'field change')}",
            target=target.id,
        )
    return None


def _check(ctx: StepContext, proposal: ChangeSetProposal, target: _Target) -> str | None:
    """``backend.check`` before proposing, so a problem can name its JSON path (SHELL-17)."""
    if proposal.changes is not None:
        assert target.current is not None
        entries = [(target.current, proposal.changes, "$.changes")]
    else:
        entries = [
            (t.target, t.changes, f"$.targets[{i}].changes")
            for i, t in enumerate(proposal.targets or [])
        ]
    targets = [
        TargetChange(
            target=ref,
            changes=[FieldChange(field=c.field, op=c.op, old=c.old, new=c.new) for c in changes],
        )
        for ref, changes, _ in entries
    ]
    probe = ChangeSet(
        id="shell-check",
        profile=ctx.profile,
        source=ChangeSource(kind="step", name="shell"),
        created_at=datetime.now(UTC),
        targets=targets,
    )
    try:
        problems = ctx.backend.check(probe)
    except CapabilityError as exc:
        return f"this profile's backend can't apply changes: {exc}"
    if not problems:
        return None
    p = problems[0]
    for ref, changes, path in entries:
        if ref != p.target:
            continue
        for j, c in enumerate(changes):
            if c.field == p.field:
                return f"invalid ChangeSet proposal at {path}[{j}].{p.field}: {p.message}"
        return f"invalid ChangeSet proposal at {path}: {p.target.label()}: {p.message}"
    return f"invalid ChangeSet proposal: {p.target.label()} {p.field}: {p.message}"


def _json_path(loc: tuple[int | str, ...]) -> str:
    path = "$"
    for part in loc:
        path += f"[{part}]" if isinstance(part, int) else f".{part}"
    return path


def _scope_name(per: str) -> str:
    return {
        "album": "per: album may change its album and its items",
        "item": "per: item may change its item only",
        "run": "per: run may change the selected albums and their items",
    }[per]


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
