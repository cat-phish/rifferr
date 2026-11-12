"""Profiles: variables, backend settings and secret references (C-PROFILE).

Pure: parsing takes text and an environment mapping. Reading profile
directories lives in ``rifferr.profile_store``; resolving secrets in
``rifferr.credentials``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PositiveInt
from pydantic_core import PydanticCustomError

from rifferr.core.workflow import VarDecl, substitute
from rifferr.core.workflow.errors import ValidationError
from rifferr.core.workflow.interpolation import InterpolationError
from rifferr.core.workflow.loader import validate_model
from rifferr.core.workflow.yamldoc import RoundTripDoc, YamlSyntaxError, parse

PROFILE_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VAR_NAME_PATTERN: Final = r"^[A-Za-z_][A-Za-z0-9_]*$"
ENV_NAME_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SECRET_REF_HELP: Final = (
    "must be keyring:<service>/<name>, env:<VAR> or file:<absolute path>, not a literal value"
)
#: Secret values shorter than this aren't redacted (they would mangle ordinary text).
MIN_REDACT_LENGTH: Final = 4


class ProfileError(ValueError):
    """A profile is missing, misnamed or unusable for the requested operation."""


class ProfileLoadError(Exception):
    """``profile.yaml`` is invalid; ``errors`` carry file locations."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))


class VarResolutionError(ProfileError):
    def __init__(self, var: str, message: str) -> None:
        self.var = var
        super().__init__(message)


def validate_profile_name(name: str) -> str:
    if not PROFILE_NAME_PATTERN.match(name):
        raise ProfileError(
            f"invalid profile name {name!r}: use lowercase letters, digits, '-' and '_' "
            "(at most 64 characters, starting with a letter or digit)"
        )
    return name


# -- secrets ------------------------------------------------------------------


class SecretRef(BaseModel):
    """Where a secret lives. Never the secret itself (PROFILE-4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["keyring", "env", "file"]
    target: str

    @classmethod
    def parse(cls, value: object) -> SecretRef:
        if isinstance(value, SecretRef):
            return value
        if isinstance(value, dict):
            return cls.model_validate(value)
        if isinstance(value, str):
            kind, sep, target = value.partition(":")
            if sep and _valid_target(kind, target):
                return cls(kind=kind, target=target)  # type: ignore[arg-type]
        raise PydanticCustomError("secret_ref", SECRET_REF_HELP)

    def __str__(self) -> str:
        return f"{self.kind}:{self.target}"


def _valid_target(kind: str, target: str) -> bool:
    if kind == "keyring":
        service, sep, name = target.partition("/")
        return bool(sep and service and name)
    if kind == "env":
        return bool(ENV_NAME_PATTERN.match(target))
    if kind == "file":
        return target.startswith("/")
    return False


class Secret:
    """A resolved secret value. Its repr and str never show the value (PROFILE-5)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __format__(self, spec: str) -> str:
        return "***"


class Redactor:
    """Masks known secret values in text bound for logs and events (PROFILE-5)."""

    def __init__(self, secrets: Iterable[Secret]) -> None:
        self._values: set[str] = set()
        self._pattern: re.Pattern[str] | None = None
        for s in secrets:
            self.add(s)

    def add(self, secret: Secret) -> None:
        """Also mask ``secret`` from now on (e.g. a secret resolved mid-run)."""
        value = secret.reveal()
        if len(value) < MIN_REDACT_LENGTH or value in self._values:
            return
        self._values.add(value)
        ordered = sorted(self._values, key=len, reverse=True)
        self._pattern = re.compile("|".join(map(re.escape, ordered)))

    def redact(self, text: str) -> str:
        return self._pattern.sub("***", text) if self._pattern else text


# -- models -------------------------------------------------------------------


DEFAULT_IDLE_TIMEOUT = 600


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idle_timeout: PositiveInt | None = Field(
        default=DEFAULT_IDLE_TIMEOUT,
        description="Seconds a worker with no run stays up; null: until server shutdown",
    )


class BeetsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    beetsdir: str | None = Field(
        default=None,
        description="beets config directory (BEETSDIR). Unset: library steps refuse to run.",
    )


class ChangeSettings(BaseModel):
    """How applied ChangeSets reach the library (C-CHANGESET CHANGESET-10)."""

    model_config = ConfigDict(extra="forbid")

    write_tags: bool = Field(
        default=True,
        description="Also write changed tags to the audio files (beets' own writer). "
        "false: change only the library database.",
    )


VarName = Annotated[str, Field(pattern=VAR_NAME_PATTERN)]
SecretRefField = Annotated[SecretRef, BeforeValidator(SecretRef.parse)]


class ProfileFile(BaseModel):
    """The contents of ``profile.yaml`` (PROFILE-2)."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "beets"
    beets: BeetsSettings = Field(default_factory=BeetsSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    changes: ChangeSettings = Field(default_factory=ChangeSettings)
    vars: dict[VarName, Annotated[str, Field(strict=True)]] = Field(default_factory=dict[str, str])
    secrets: dict[str, SecretRefField] = Field(default_factory=dict[str, SecretRef])
    timezone: str | None = Field(default=None, description="IANA time zone; default: system")


class Profile(ProfileFile):
    """A loaded profile: file contents with resolved ``vars``."""

    name: str
    root: Path

    def require_beetsdir(self) -> Path:
        if not self.beets.beetsdir:
            raise ProfileError(
                f"profile '{self.name}' has no beets.beetsdir; set it in "
                f"{self.root / 'profile.yaml'} to the beets config directory to use"
            )
        return Path(self.beets.beetsdir)


# -- parsing ------------------------------------------------------------------


def parse_profile(
    text: str,
    source: str,
    *,
    name: str,
    root: Path,
    env: Mapping[str, str],
    timezone_exists: Callable[[str], bool] | None = None,
) -> Profile:
    validate_profile_name(name)
    try:
        doc = parse(text, source)
    except YamlSyntaxError as exc:
        raise ProfileLoadError([exc.error]) from exc
    data: Any = doc.plain()
    if data is None:
        data = {}
    file, errors = validate_model(ProfileFile, data, doc, ())
    if file is None:
        raise ProfileLoadError(errors)
    errors = _semantic_errors(file, doc, env, timezone_exists)
    if errors:
        raise ProfileLoadError(errors)
    return Profile(
        backend=file.backend,
        beets=file.beets,
        worker=file.worker,
        changes=file.changes,
        vars=resolve_vars(file.vars, env),
        secrets=file.secrets,
        timezone=file.timezone,
        name=name,
        root=root,
    )


def _semantic_errors(
    file: ProfileFile,
    doc: RoundTripDoc,
    env: Mapping[str, str],
    timezone_exists: Callable[[str], bool] | None,
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    try:
        resolve_vars(file.vars, env)
    except VarResolutionError as exc:
        errors.append(doc.error(("vars", exc.var), str(exc)))
    if file.timezone and timezone_exists and not timezone_exists(file.timezone):
        errors.append(doc.error(("timezone",), f"unknown time zone {file.timezone!r}"))
    return errors


def resolve_vars(raw: Mapping[str, str], env: Mapping[str, str]) -> dict[str, str]:
    """Resolve ``{{ vars.x }}`` and ``{{ env.X }}`` inside profile vars (PROFILE-7)."""
    done: dict[str, str] = {}

    def visit(name: str, stack: list[str]) -> str:
        if name in done:
            return done[name]
        if name in stack:
            cycle = " -> ".join([*stack[stack.index(name) :], name])
            raise VarResolutionError(stack[0], f"variable cycle: {cycle}")

        def lookup(path: tuple[str, ...]) -> str:
            if len(path) == 2 and path[0] == "vars":
                if path[1] not in raw:
                    raise VarResolutionError(name, f"{name}: unknown variable '{path[1]}'")
                return visit(path[1], [*stack, name])
            if len(path) == 2 and path[0] == "env":
                if path[1] not in env:
                    raise VarResolutionError(
                        name, f"{name}: environment variable '{path[1]}' is not set"
                    )
                return env[path[1]]
            raise VarResolutionError(
                name, f"{name}: profile vars may use only vars.<name> and env.<NAME>"
            )

        try:
            value = substitute(raw[name], lookup)
        except InterpolationError as exc:
            raise VarResolutionError(name, f"{name}: {exc}") from exc
        done[name] = value
        return value

    return {name: visit(name, []) for name in raw}


def merged_vars(profile_vars: Mapping[str, str], decls: Mapping[str, VarDecl]) -> dict[str, str]:
    """Workflow defaults, overridden by profile values (PROFILE-6)."""
    defaults = {k: d.default for k, d in decls.items() if d.default is not None}
    return {**defaults, **profile_vars}


def env_var_name(var: str) -> str:
    """Environment name for a var in shell steps: ``music_dir`` -> ``RIFFERR_VAR_MUSIC_DIR``."""
    return "RIFFERR_VAR_" + re.sub(r"[^A-Za-z0-9]", "_", var).upper()
