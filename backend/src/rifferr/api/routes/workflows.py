from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Query

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    CalledWorkflow,
    SafetyFinding,
    TrustRequest,
    TrustResult,
    ValidateResult,
    VarInfo,
    WorkflowDetail,
    WorkflowError,
    WorkflowSummary,
)
from rifferr.api.state import ServerState, State
from rifferr.beets_config import enabled_plugins
from rifferr.core.profile import Profile, ProfileError, ProfileLoadError, env_backed_vars
from rifferr.core.workflow import (
    WORKFLOW_SUFFIXES,
    Calls,
    ValidationError,
    Workflow,
    WorkflowLoadError,
    load_workflow,
    safety_report,
    validate,
)
from rifferr.core.workflow.yamldoc import YamlSyntaxError
from rifferr.core.workflow.yamldoc import parse as parse_yaml
from rifferr.profile_store import WORKFLOWS_DIR, load_profile, profile_dir
from rifferr.trust import WorkflowFileError, read_workflow_file
from rifferr.workflow_calls import called_trust, read_calls

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@dataclass
class Checked:
    name: str
    path: Path
    text: str
    sha256: str
    workflow: Workflow | None
    errors: list[ValidationError]
    vars: list[VarInfo] = field(default_factory=list[VarInfo])
    missing_vars: list[str] = field(default_factory=list[str])
    calls: Calls | None = None
    """The files it calls, read with it (S-SUBWORKFLOW)."""
    profile: str = ""
    """The profile it was read from: trust is per profile (SEC-63)."""

    def called(self, state: ServerState) -> list[CalledWorkflow]:
        return [
            CalledWorkflow(path=c.path, sha256=c.sha256, trusted=c.trusted, yaml=c.text)
            for c in called_trust(state.db, self.calls, self.profile)
        ]

    def trusted(self, state: ServerState) -> bool:
        """The run would be trusted: this file and every file it calls (SUBWORKFLOW-13)."""
        if not self.sha256 or not state.db.is_trusted(self.sha256, self.profile):
            return False
        return all(c.trusted for c in called_trust(state.db, self.calls, self.profile))


def get_profile(state: ServerState, name: str) -> Profile:
    try:
        return load_profile(state.config_root, name)
    except ProfileError as exc:
        raise ApiError(404, "not_found", str(exc)) from None
    except ProfileLoadError as exc:
        raise ApiError(422, "validation_failed", f"profile '{name}' is invalid: {exc}") from None


def workflow_files(state: ServerState, profile: str) -> dict[str, list[Path]]:
    folder = profile_dir(state.config_root, profile) / WORKFLOWS_DIR
    files: dict[str, list[Path]] = {}
    if folder.is_dir():
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.suffix in WORKFLOW_SUFFIXES:
                files.setdefault(f.stem, []).append(f)
    return files


def find_workflow(state: ServerState, profile: str, name: str) -> Path:
    """Resolve a workflow by file stem (API-3)."""
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ApiError(400, "invalid_workflow_name", f"invalid workflow name {name!r}")
    matches = workflow_files(state, profile).get(name, [])
    if not matches:
        raise not_found(f"workflow '{name}' in profile '{profile}'")
    if len(matches) > 1:
        names = ", ".join(p.name for p in matches)
        raise ApiError(409, "ambiguous_workflow_name", f"'{name}' matches {names}")
    return matches[0]


ENV_PLACEHOLDER = "(set by the profile)"


def _env_backed(state: ServerState, profile: Profile) -> set[str]:
    """SEC-47: vars filled from the environment are never echoed (they may hold secrets)."""
    try:
        doc = parse_yaml(
            (profile_dir(state.config_root, profile.name) / "profile.yaml").read_text("utf-8"), ""
        )
        raw: Any = doc.plain()
        vars_ = raw.get("vars") if isinstance(raw, dict) else None
    except (OSError, YamlSyntaxError):
        return set(profile.vars)  # cannot tell: hide all
    if not isinstance(vars_, dict):
        return set()
    return env_backed_vars({str(k): str(v) for k, v in vars_.items()})


def check(
    state: ServerState,
    profile: Profile,
    name: str,
    path: Path,
    run_vars: dict[str, str],
    *,
    for_start: bool = False,
) -> Checked:
    """Load and validate without backend capabilities: the server never loads beets.

    Unless ``for_start``, vars that are supplied at start (no default, not set by the
    profile) are reported as ``missing_vars`` instead of errors (API-16).
    """
    pn = profile.name
    try:
        text, sha = read_workflow_file(path)
    except WorkflowFileError as exc:
        return Checked(
            name, path, "", "", None, [ValidationError(str(path), "", str(exc))], profile=pn
        )
    try:
        wf, doc = load_workflow(text, str(path))
    except WorkflowLoadError as exc:
        return Checked(name, path, text, sha, None, list(exc.errors), profile=pn)
    known = {**profile.vars, **run_vars}
    hidden = _env_backed(state, profile)
    infos = [
        VarInfo(
            name=n,
            default=d.default,
            description=d.description,
            profile_value=ENV_PLACEHOLDER if n in hidden else profile.vars.get(n),
        )
        for n, d in wf.vars.items()
    ]
    missing = [n for n, d in wf.vars.items() if d.default is None and n not in known]
    if not for_start:
        known |= dict.fromkeys(missing, "")  # placeholders: supplied at start
    calls = read_calls(profile_dir(state.config_root, profile.name) / WORKFLOWS_DIR, wf, doc, path)
    errors = validate(wf, state.registry, None, known, doc=doc, calls=calls)
    return Checked(
        name, path, text, sha, wf, errors, infos, [] if for_start else missing, calls, pn
    )


def error_out(errors: list[ValidationError]) -> list[WorkflowError]:
    return [
        WorkflowError(path=e.path, message=e.message, line=e.line, column=e.column) for e in errors
    ]


@router.get("", response_model=list[WorkflowSummary], responses=ERROR_RESPONSES)
async def list_workflows(profile: Annotated[str, Query()], state: State) -> list[WorkflowSummary]:
    p = get_profile(state, profile)
    out: list[WorkflowSummary] = []
    for name, paths in workflow_files(state, profile).items():
        for path in paths:
            c = check(state, p, name, path, {})
            out.append(
                WorkflowSummary(
                    name=name,
                    path=str(path),
                    sha256=c.sha256,
                    valid=not c.errors and len(paths) == 1,
                    trusted=c.trusted(state),
                    vars=c.vars,
                    missing_vars=c.missing_vars,
                )
            )
    return out


@router.get("/{name}", response_model=WorkflowDetail, responses=ERROR_RESPONSES)
async def get_workflow(name: str, profile: Annotated[str, Query()], state: State) -> WorkflowDetail:
    p = get_profile(state, profile)
    c = check(state, p, name, find_workflow(state, profile, name), {})
    return WorkflowDetail(
        name=name,
        path=str(c.path),
        sha256=c.sha256,
        valid=not c.errors,
        trusted=c.trusted(state),
        yaml=c.text,
        errors=error_out(c.errors),
        vars=c.vars,
        missing_vars=c.missing_vars,
        called=c.called(state),
    )


@router.post("/{name}/validate", response_model=ValidateResult, responses=ERROR_RESPONSES)
async def validate_workflow(
    name: str, profile: Annotated[str, Query()], state: State
) -> ValidateResult:
    """Validation errors plus the static safety report (API-4, SECURITY-11)."""
    p = get_profile(state, profile)
    c = check(state, p, name, find_workflow(state, profile, name), {})
    plugins = enabled_plugins(p.beets.beetsdir) if c.workflow is not None else []
    findings = (
        safety_report(c.workflow, c.calls, beets_plugins=plugins).findings
        if c.workflow is not None
        else []
    )
    return ValidateResult(
        valid=not c.errors,
        errors=error_out(c.errors),
        safety=[
            SafetyFinding(step_id=f.step_id, kind=f.kind, detail=f.detail, workflow=f.workflow)
            for f in findings
        ],
        sha256=c.sha256,
        trusted=c.trusted(state),
        called=c.called(state),
    )


@router.post("/{name}/trust", response_model=TrustResult, responses=ERROR_RESPONSES)
async def trust_workflow(
    name: str,
    body: TrustRequest,
    profile: Annotated[str, Query()],
    state: State,
) -> TrustResult:
    """Trust exactly the content the user reviewed (API-14, SECURITY-12), with the
    called files it lists, each by its own hash (SUBWORKFLOW-13)."""
    p = get_profile(state, profile)
    path = find_workflow(state, profile, name)
    c = check(state, p, name, path, {})
    if not c.sha256 or c.errors:  # SEC-64: the same rule as `rifferr trust --profile`
        raise ApiError(422, "validation_failed", "; ".join(str(e) for e in c.errors))
    if c.sha256 != body.sha256:
        raise ApiError(
            409, "hash_mismatch", "the workflow changed since it was reviewed; review it again"
        )
    called = {f.path: f for f in called_trust(state.db, c.calls, profile)}
    for f in called.values():
        if not f.trusted and body.called.get(f.path) != f.sha256:
            raise ApiError(
                409,
                "hash_mismatch",
                f"called workflow '{f.path}' changed or wasn't reviewed; review it again",
            )
    for rel, sha in body.called.items():
        if rel not in called or called[rel].sha256 != sha:
            raise ApiError(
                409, "hash_mismatch", f"called workflow '{rel}' changed since it was reviewed"
            )
    workflows = profile_dir(state.config_root, profile) / WORKFLOWS_DIR
    state.db.trust(c.sha256, str(path), "ui", profile)  # SEC-63: in this profile only
    for rel, sha in body.called.items():
        state.db.trust(sha, str(workflows / rel), "ui", profile)
    if state.triggers is not None:  # TRIGGERS-2: arm now, not at the next check
        await state.triggers.reconcile()
    return TrustResult(name=name, sha256=c.sha256, called=dict(body.called))
