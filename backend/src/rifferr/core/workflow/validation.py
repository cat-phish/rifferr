"""Semantic workflow validation (WORKFLOW-2, -5 to -8, -10)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
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
) -> list[ValidationError]:
    """Every problem with ``wf``. Profile-dependent checks run only when
    ``profile_vars`` is given; capability checks only when ``backend_caps`` is."""
    d = doc or RoundTripDoc.detached()
    errors: list[ValidationError] = []
    errors += _check_steps(wf, registry, backend_caps, d)
    errors += _check_expressions(wf, profile_vars, d)
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


def _strings(value: Any, path: Path) -> Iterator[tuple[Path, str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for k, v in cast(Mapping[str, Any], value).items():
            yield from _strings(v, (*path, k))
    elif isinstance(value, list):
        for i, v in enumerate(cast(list[Any], value)):
            yield from _strings(v, (*path, i))


def _expression_sites(wf: Workflow) -> Iterator[tuple[int | None, Path, str]]:
    """(index of the step it belongs to, path, string) for every interpolated string."""
    if wf.on is not None:
        for path, s in _strings(wf.on.model_dump(exclude_none=True), ("on",)):
            yield None, path, s
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
    if root == "steps":
        if len(rest) != 2:
            return f"'{text}': use steps.<id>.<key>"
        target = step_index.get(rest[0])
        if target is None or owner is None or target >= owner:
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
