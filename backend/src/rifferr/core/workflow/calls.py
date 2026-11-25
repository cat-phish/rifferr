"""Workflow calls (S-SUBWORKFLOW): ``uses: workflow`` steps, their files, and expansion.

A call runs inside the caller's run: at submit, each call step is replaced by the called
workflow's steps (SUBWORKFLOW-6). Everything here is pure; called files come from a
``read`` callable, so ``rifferr validate``, the API and the worker share it.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from rifferr.core.workflow.errors import Path, ValidationError, WorkflowLoadError
from rifferr.core.workflow.interpolation import (
    Expression,
    InterpolationError,
    escape,
    find_expressions,
    rewrite,
)
from rifferr.core.workflow.loader import load_workflow
from rifferr.core.workflow.models import StepSpec, Workflow
from rifferr.core.workflow.yamldoc import RoundTripDoc

CALL_TYPE: Final = "workflow"
"""The step type that calls another workflow."""
MAX_DEPTH: Final = 8
"""How deep calls may nest (SUBWORKFLOW-5)."""
SEPARATOR: Final = "/"
"""Joins the call path into an expanded step id (``tagging/fix-case``)."""

Scalar = str | int | float | bool


class WorkflowCallParams(BaseModel):
    """``with:`` of a ``uses: workflow`` step (SUBWORKFLOW-1, -2)."""

    model_config = ConfigDict(extra="forbid")

    workflow: str = Field(description="Workflow file, relative to the profile's workflows/ folder")
    vars: dict[str, Scalar] = Field(
        default_factory=dict[str, Scalar],
        description="Values for the called workflow's declared vars",
    )


class CalledStep(StepSpec):
    """A step that came from a called workflow, after expansion (SUBWORKFLOW-6).

    ``id`` is the expanded id. A step with ``uses: workflow`` here is the end of a
    call whose workflow declares ``outputs``; it runs after the called steps and
    returns those outputs (SUBWORKFLOW-11).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str  # pyright: ignore[reportIncompatibleVariableOverride]
    call_path: tuple[str, ...] = ()
    """Ids of the call steps around this step, outermost first (the end of a call
    counts as inside it)."""
    workflow: str = ""
    """The called file this step comes from, relative to workflows/."""
    workflow_name: str = ""
    """That file's ``name``, a label for the run page (SUBWORKFLOW-10)."""
    narrow: tuple[str, ...] = ()
    """The ``when`` of every call around this step (SUBWORKFLOW-7)."""
    scope: dict[str, str] = Field(default_factory=dict[str, str])
    """The called workflow's vars as bound by the call: name -> replacement text
    (SUBWORKFLOW-2). Used for text the step reads at run time, such as script files."""
    outputs: dict[str, str] = Field(default_factory=dict[str, str])
    """End of a call only: output name -> expression in the run's step ids."""


@dataclass(frozen=True)
class CalledFile:
    """One called workflow file as read at submit (SUBWORKFLOW-12)."""

    path: str
    text: str
    workflow: Workflow | None
    doc: RoundTripDoc | None
    errors: list[ValidationError] = field(default_factory=list[ValidationError])


@dataclass(frozen=True)
class Calls:
    """Every file a workflow calls, directly or not, and the problems finding them."""

    files: dict[str, CalledFile] = field(default_factory=dict[str, CalledFile])
    errors: list[ValidationError] = field(default_factory=list[ValidationError])

    def get(self, path: str) -> Workflow | None:
        f = self.files.get(path)
        return f.workflow if f is not None else None

    def texts(self) -> dict[str, str]:
        return {p: f.text for p, f in self.files.items()}


ReadFile = Callable[[str], str]
"""Text of a file relative to the profile's workflows/ folder. Raises
``FileNotFoundError`` when there is none, ``OSError`` or ``ValueError`` when it
can't be read."""


def path_problem(path: str) -> str | None:
    """Why ``path`` can't name a called workflow, or None (SUBWORKFLOW-1)."""
    if "{{" in path:
        return f"workflow '{path}' must be a fixed path, not a '{{{{ }}}}' expression"
    if not path.strip():
        return "workflow path is empty"
    if path.startswith("/") or path.startswith("\\") or posixpath.isabs(path) or ":" in path[:3]:
        return f"workflow '{path}' must be relative to the profile's workflows/ folder"
    norm = posixpath.normpath(path.replace("\\", "/"))
    if norm == ".." or norm.startswith("../"):
        return f"workflow '{path}' leaves the profile's workflows/ folder"
    if norm == ".":
        return f"workflow '{path}' names the folder, not a file"
    return None


def normalize(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/"))


def call_target(step: StepSpec) -> str | None:
    """The normalized file a call step names, or None when the path is unusable."""
    if step.uses != CALL_TYPE:
        return None
    path = step.with_.get("workflow")
    if not isinstance(path, str) or path_problem(path) is not None:
        return None
    return normalize(path)


def resolve_calls(
    wf: Workflow,
    doc: RoundTripDoc | None,
    read: ReadFile,
    *,
    name: str | None = None,
) -> Calls:
    """Read every file ``wf`` calls, recursively, and find cycles and depth problems
    (SUBWORKFLOW-1, -5). ``name``: ``wf``'s own path relative to workflows/, when it
    lives there, so a call back to it is seen as a cycle."""
    calls = Calls()
    root = normalize(name) if name else "<workflow>"
    _walk(wf, doc or RoundTripDoc.detached(), read, calls, (root,))
    return calls


def _walk(
    wf: Workflow, doc: RoundTripDoc, read: ReadFile, calls: Calls, stack: tuple[str, ...]
) -> None:
    for i, step in enumerate(wf.steps):
        if step.uses != CALL_TYPE:
            continue
        raw = step.with_.get("workflow")
        if not isinstance(raw, str):
            continue  # the parameter check reports it
        where: Path = ("steps", i, "with", "workflow")
        problem = path_problem(raw)
        if problem is not None:
            calls.errors.append(doc.error(where, problem))
            continue
        target = normalize(raw)
        if target in stack:
            cycle = " → ".join((*stack[stack.index(target) :], target))
            calls.errors.append(doc.error(where, f"workflow call cycle: {cycle}"))
            continue
        if len(stack) > MAX_DEPTH:
            calls.errors.append(doc.error(where, f"workflow calls nest more than {MAX_DEPTH} deep"))
            continue
        if target not in calls.files:
            loaded = _load(target, read)
            if loaded is None:
                calls.errors.append(
                    doc.error(
                        where, f"workflow '{raw}' not found in the profile's workflows/ folder"
                    )
                )
                continue
            calls.files[target] = loaded
        called = calls.files[target]
        if called.workflow is not None and called.doc is not None:
            _walk(called.workflow, called.doc, read, calls, (*stack, target))


def _load(path: str, read: ReadFile) -> CalledFile | None:
    try:
        text = read(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        return CalledFile(path, "", None, None, [ValidationError(path, "", str(exc))])
    try:
        wf, doc = load_workflow(text, path)
    except WorkflowLoadError as exc:
        return CalledFile(path, text, None, None, list(exc.errors))
    return CalledFile(path, text, wf, doc)


def stored_reader(texts: Mapping[str, str]) -> ReadFile:
    """A ``read`` over texts stored with a job (SUBWORKFLOW-12)."""

    def read(path: str) -> str:
        if path not in texts:
            raise FileNotFoundError(path)
        return texts[path]

    return read


# -- expansion (SUBWORKFLOW-6) -------------------------------------------------------


def expand(wf: Workflow, calls: Calls) -> Workflow:
    """``wf`` with each call replaced by the called steps, recursively.

    Meant for validated workflows; a call whose file didn't load stays as written.
    Inside called steps, ``{{ vars.X }}`` becomes the value the call passes (an
    expression in the caller's context, SUBWORKFLOW-3) or the var's default, and
    ``{{ steps.<id>.<key> }}`` the expanded id (SUBWORKFLOW-4).
    """
    steps = _expand_steps(wf, calls, (), "", {}, ())
    return wf.model_copy(update={"steps": steps})


Rewrite = Callable[[Expression], str]


def _expand_steps(
    wf: Workflow,
    calls: Calls,
    path: tuple[str, ...],
    file: str,
    bound: Mapping[str, str],
    narrow: tuple[str, ...],
) -> list[StepSpec]:
    """``bound``: var name -> replacement text for the called workflow at ``path``."""
    inner = _scope_rewriter(path, bound)

    out: list[StepSpec] = []
    for step in wf.steps:
        target = call_target(step)
        if target is None:
            out.append(
                _rewritten(step, path, (file, wf.name), narrow, inner, bound) if path else step
            )
            continue
        called = calls.get(target)
        try:
            params = WorkflowCallParams.model_validate(step.with_)
        except PydanticValidationError:
            params = None
        if called is None or params is None:  # not valid; validation reports it. Kept as written.
            out.append(step)
            continue
        values: dict[str, str] = {}
        for var, decl in called.vars.items():
            if var in params.vars:
                value = params.vars[var]
                values[var] = rewrite(value, inner) if isinstance(value, str) else _render(value)
            elif decl.default is not None:
                values[var] = escape(decl.default)
        when = (*narrow, rewrite(step.when, inner)) if step.when else narrow
        call_path = (*path, step.id)
        prefix = SEPARATOR.join(path) + SEPARATOR if path else ""
        out += _expand_steps(called, calls, call_path, target, values, when)
        if called.outputs:
            ends = SEPARATOR.join(call_path) + SEPARATOR
            out.append(
                CalledStep(
                    id=prefix + step.id,
                    uses=CALL_TYPE,
                    name=step.name,
                    with_={"workflow": target},  # pyright: ignore[reportCallIssue]
                    call_path=call_path,
                    workflow=target,
                    workflow_name=called.name,
                    outputs={
                        k: rewrite(v, lambda e, ends=ends: _scoped(e, ends))
                        for k, v in called.outputs.items()
                    },
                )
            )
    return out


def _scoped(expr: Expression, prefix: str) -> str:
    root, *rest = expr.path
    if root == "steps" and len(rest) == 2:
        return "{{ steps." + prefix + rest[0] + "." + rest[1] + " }}"
    return expr.text


def _scope_rewriter(path: tuple[str, ...], bound: Mapping[str, str]) -> Rewrite:
    """Rewrites an expression written inside the called workflow at ``path`` into the
    run's context: its vars to what the call bound (SUBWORKFLOW-2), its step ids to the
    expanded ids (SUBWORKFLOW-4)."""
    prefix = SEPARATOR.join(path) + SEPARATOR if path else ""

    def inner(expr: Expression) -> str:
        root, *rest = expr.path
        if path and root == "vars" and len(rest) == 1 and rest[0] in bound:
            return bound[rest[0]]
        if path and root == "steps" and len(rest) == 2:
            return "{{ steps." + prefix + rest[0] + "." + rest[1] + " }}"
        return expr.text

    return inner


def in_call_scope(step: StepSpec, text: str) -> str:
    """``text`` that ``step`` reads at run time (a script file, a rules file), with its
    ``{{ }}`` expressions scoped like the step's own ``with:`` values. Unchanged for a
    step of the top-level workflow, and for the end of a call, whose ``outputs`` are
    already in the run's step ids."""
    if not isinstance(step, CalledStep) or not step.call_path or step.uses == CALL_TYPE:
        return text
    return rewrite(text, _scope_rewriter(step.call_path, step.scope))


def _rewritten(
    step: StepSpec,
    path: tuple[str, ...],
    file: tuple[str, str],
    narrow: tuple[str, ...],
    fn: Rewrite,
    bound: Mapping[str, str],
) -> CalledStep:
    return CalledStep(
        id=SEPARATOR.join((*path, step.id)),
        uses=step.uses,
        name=step.name,
        with_=_rewrite_values(step.with_, fn),  # pyright: ignore[reportCallIssue]
        when=rewrite(step.when, fn) if step.when is not None else None,
        on_error=step.on_error,
        call_path=path,
        workflow=file[0],
        workflow_name=file[1],
        narrow=narrow,
        scope=dict(bound),
        **({"apply": step.apply} if "apply" in step.model_fields_set else {}),
    )


def _rewrite_values(value: Any, fn: Rewrite) -> Any:
    if isinstance(value, str):
        return rewrite(value, fn)
    if isinstance(value, dict):
        return {k: _rewrite_values(v, fn) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_rewrite_values(v, fn) for v in cast(list[Any], value)]
    return value


def _render(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return escape(str(value))


def output_names(step: StepSpec, calls: Calls | None) -> set[str] | None:
    """Outputs a call step offers (SUBWORKFLOW-11); None when not a call or not known."""
    if calls is None:
        return None
    target = call_target(step)
    called = calls.get(target) if target is not None else None
    return set(called.outputs) if called is not None else None


def output_problems(wf: Workflow, calls: Calls | None) -> list[tuple[str, str]]:
    """(output name, problem) for ``outputs:`` that aren't ``{{ steps.<id>.<key> }}``
    over this workflow's own steps."""
    ids = {s.id: s for s in wf.steps}
    problems: list[tuple[str, str]] = []
    for name, value in wf.outputs.items():
        try:
            exprs = find_expressions(value)
        except InterpolationError as exc:
            problems.append((name, str(exc)))
            continue
        if not exprs:
            problems.append((name, f"output '{name}' must use {{{{ steps.<id>.<key> }}}}"))
        for expr in exprs:
            root, *rest = expr.path
            if root != "steps" or len(rest) != 2:
                problems.append((name, f"'{expr.text}': outputs may only use steps.<id>.<key>"))
            elif rest[0] not in ids:
                problems.append((name, f"'{expr.text}': no step '{rest[0]}' in this workflow"))
            else:
                offered = output_names(ids[rest[0]], calls)
                if offered is not None and rest[1] not in offered:
                    file = call_target(ids[rest[0]])
                    problems.append((name, f"'{expr.text}': {file} declares no output '{rest[1]}'"))
    return problems


__all__ = [
    "CALL_TYPE",
    "MAX_DEPTH",
    "SEPARATOR",
    "CalledFile",
    "CalledStep",
    "Calls",
    "ReadFile",
    "WorkflowCallParams",
    "call_target",
    "expand",
    "normalize",
    "output_names",
    "output_problems",
    "path_problem",
    "resolve_calls",
    "stored_reader",
]
