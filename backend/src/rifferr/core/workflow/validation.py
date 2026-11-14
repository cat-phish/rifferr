"""Semantic workflow validation (WORKFLOW-2, -5 to -8, -10)."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from typing import Any, cast

from rifferr.backends.base import BackendCapabilities
from rifferr.core.workflow.errors import Path, ValidationError
from rifferr.core.workflow.interpolation import (
    PROFILE_FIELDS,
    ROOTS,
    RUN_FIELDS,
    Expression,
    InterpolationError,
    find_expressions,
)
from rifferr.core.workflow.loader import validate_model
from rifferr.core.workflow.models import Workflow
from rifferr.core.workflow.registry import StepRegistry
from rifferr.core.workflow.yamldoc import RoundTripDoc


def validate(
    wf: Workflow,
    registry: StepRegistry,
    backend_caps: BackendCapabilities | None = None,
    profile_vars: Mapping[str, str] | None = None,
    *,
    doc: RoundTripDoc | None = None,
    notify_targets: Collection[str] | None = None,
) -> list[ValidationError]:
    """Every problem with ``wf``. Profile-dependent checks run only when
    ``profile_vars`` is given; capability checks only when ``backend_caps`` is;
    notification target names only when ``notify_targets`` is (S-NOTIFY NOTIFY-13)."""
    d = doc or RoundTripDoc.detached()
    errors: list[ValidationError] = []
    errors += _check_steps(wf, registry, backend_caps, d)
    errors += _check_expressions(wf, profile_vars, d)
    if notify_targets is not None:
        errors += _check_notify_targets(wf, registry, notify_targets, d)
    if profile_vars is not None:
        for name, decl in wf.vars.items():
            if decl.default is None and name not in profile_vars:
                errors.append(
                    d.error(
                        ("vars", name),
                        "variable has no default and the profile doesn't set it",
                        "key",
                    )
                )
    return sorted(errors, key=lambda e: (e.line or 0, e.column or 0, e.path))


def _check_steps(
    wf: Workflow, registry: StepRegistry, caps: BackendCapabilities | None, d: RoundTripDoc
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    first_seen: dict[str, int] = {}
    for i, step in enumerate(wf.steps):
        if step.id in first_seen:
            errors.append(
                d.error(
                    ("steps", i, "id"),
                    f"duplicate step id '{step.id}' (first used by steps[{first_seen[step.id]}])",
                )
            )
        else:
            first_seen[step.id] = i
        info = registry.get(step.uses)
        if info is None:
            known = ", ".join(registry.names()) or "none registered"
            errors.append(
                d.error(("steps", i, "uses"), f"unknown step type '{step.uses}' (known: {known})")
            )
            continue
        _, param_errors = validate_model(info.params_model, step.with_, d, ("steps", i, "with"))
        errors += param_errors
        if not info.accepts_when and step.when is not None:
            errors.append(
                d.error(
                    ("steps", i, "when"),
                    f"step type '{step.uses}' works on a folder, not on albums; "
                    "'when' is not allowed for it",
                    "key",
                )
            )
        if info.force_review and step.apply == "auto":
            errors.append(
                d.error(
                    ("steps", i, "apply"),
                    f"step type '{step.uses}' always waits for review; "
                    "'apply: auto' is not allowed for it",
                )
            )
        if caps is not None:
            for cap in caps.missing(info.requires):
                errors.append(
                    d.error(
                        ("steps", i, "uses"),
                        f"step type '{step.uses}' needs backend capability '{cap}', "
                        "which this profile's backend doesn't offer",
                    )
                )
    return errors


NOTIFY_TARGET_REF = "notify-target"
"""``json_schema_extra={"x-rifferr-ref": NOTIFY_TARGET_REF}`` marks a step parameter
(a list of names) whose values must be the profile's notification targets."""


def _check_notify_targets(
    wf: Workflow, registry: StepRegistry, known: Collection[str], d: RoundTripDoc
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    for i, step in enumerate(wf.steps):
        info = registry.get(step.uses)
        if info is None:
            continue
        for name, field in info.params_model.model_fields.items():
            extra = field.json_schema_extra
            if not isinstance(extra, dict):
                continue
            if cast(dict[str, Any], extra).get("x-rifferr-ref") != NOTIFY_TARGET_REF:
                continue
            key = field.alias or name
            value = step.with_.get(key)
            values = [value] if isinstance(value, str) else value
            if not isinstance(values, list):
                continue
            for j, v in enumerate(cast(list[Any], values)):
                if isinstance(v, str) and "{{" not in v and v not in known:
                    names = ", ".join(sorted(known)) or "none"
                    errors.append(
                        d.error(
                            ("steps", i, "with", key, j),
                            f"unknown notification target '{v}' (this profile has: {names})",
                        )
                    )
    return errors


def _strings(value: Any, path: Path) -> Iterator[tuple[Path, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for k, v in cast(Mapping[str, Any], value).items():
            yield from _strings(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(cast(list[Any], value)):
            yield from _strings(v, (*path, i))


#: Owner of strings interpolated when triggers are armed (F-TRIGGERS TRIGGERS-3).
ARM_TIME = -1
ARM_TIME_ROOTS = ("vars", "profile")


def arm_time_sites(wf: Workflow) -> Iterator[tuple[Path, str]]:
    """Strings resolved when the workflow's triggers are armed: ``input``,
    ``on.watch.path`` and ``on.webhook.within``. Other ``on`` fields aren't templates."""
    if wf.input is not None:
        yield ("input",), wf.input
    if wf.on is not None:
        if wf.on.watch is not None:
            yield ("on", "watch", "path"), wf.on.watch.path
        if wf.on.webhook is not None and wf.on.webhook.within is not None:
            yield ("on", "webhook", "within"), wf.on.webhook.within


def _expression_sites(wf: Workflow) -> Iterator[tuple[int | None, Path, str]]:
    """(index of the step it belongs to, path, string) for every interpolated string."""
    for path, s in arm_time_sites(wf):
        yield ARM_TIME, path, s
    for i, step in enumerate(wf.steps):
        yield from ((i, p, s) for p, s in _strings(step.with_, ("steps", i, "with")))
        if step.when is not None:
            yield i, ("steps", i, "when"), step.when


def _check_expressions(
    wf: Workflow, profile_vars: Mapping[str, str] | None, d: RoundTripDoc
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    step_index = {s.id: i for i, s in reversed(list(enumerate(wf.steps)))}
    for owner, path, value in _expression_sites(wf):
        try:
            expressions = find_expressions(value)
        except InterpolationError as exc:
            errors.append(d.error(path, str(exc)))
            continue
        for expr in expressions:
            problem = _expression_problem(expr, owner, step_index, wf, profile_vars)
            if problem:
                errors.append(d.error(path, problem))
    return errors


def _expression_problem(
    expr: Expression,
    owner: int | None,
    step_index: Mapping[str, int],
    wf: Workflow,
    profile_vars: Mapping[str, str] | None,
) -> str | None:
    root, *rest = expr.path
    text = expr.text
    if root not in ROOTS:
        return f"unknown root '{root}' in '{text}' (allowed: {', '.join(ROOTS)})"
    if owner == ARM_TIME and root not in ARM_TIME_ROOTS:
        return (
            f"'{text}' can't be used here: this value is resolved before any run exists "
            f"(allowed roots: {', '.join(ARM_TIME_ROOTS)})"
        )
    if root == "steps":
        if len(rest) != 2:
            return f"'{text}': use steps.<id>.<key>"
        target = step_index.get(rest[0])
        if target is None or owner is None or owner < 0 or target >= owner:
            return f"'{text}' must refer to an earlier step"
        return None
    if len(rest) != 1:
        return f"'{text}': use {root}.<name>"
    name = rest[0]
    if root == "run" and name not in RUN_FIELDS:
        return f"unknown field '{name}' for 'run' in '{text}' (allowed: {', '.join(RUN_FIELDS)})"
    if root == "profile" and name not in PROFILE_FIELDS:
        return (
            f"unknown field '{name}' for 'profile' in '{text}' "
            f"(allowed: {', '.join(PROFILE_FIELDS)})"
        )
    if (
        root == "vars"
        and profile_vars is not None
        and name not in wf.vars
        and name not in profile_vars
    ):
        return f"variable '{name}' is not declared in vars and not set by the profile"
    return None
