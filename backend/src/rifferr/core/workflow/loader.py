"""Load a workflow file into models with located errors (WORKFLOW-1, -4)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from rifferr.core.workflow.errors import Path, ValidationError, WorkflowLoadError
from rifferr.core.workflow.models import Workflow
from rifferr.core.workflow.yamldoc import LocateMode, RoundTripDoc, YamlSyntaxError, parse


def load_workflow(text: str, source: str = "<string>") -> tuple[Workflow, RoundTripDoc]:
    """Parse and schema-check a workflow; raise ``WorkflowLoadError`` with every problem."""
    try:
        doc = parse(text, source)
    except YamlSyntaxError as exc:
        raise WorkflowLoadError([exc.error]) from exc
    workflow, errors = validate_model(Workflow, doc.plain(), doc, ())
    if workflow is None:
        raise WorkflowLoadError(errors)
    return workflow, doc


def validate_model[M: BaseModel](
    model: type[M], data: Any, doc: RoundTripDoc, base: Path
) -> tuple[M | None, list[ValidationError]]:
    """Validate ``data`` (found at ``base`` in ``doc``) against ``model``."""
    try:
        return model.model_validate(data), []
    except PydanticValidationError as exc:
        return None, located_errors(exc, data, doc, base)


def located_errors(
    exc: PydanticValidationError, data: Any, doc: RoundTripDoc, base: Path
) -> list[ValidationError]:
    merged: dict[tuple[Path, LocateMode], list[str]] = {}
    for err in exc.errors(include_url=False):
        loc = _trim_loc(tuple(err["loc"]), data)
        kind = str(err["type"])
        if kind == "extra_forbidden":
            merged.setdefault((base + loc, "key"), []).append("unknown key")
        elif kind == "missing":
            merged.setdefault((base + loc, "nearest"), []).append(str(err["msg"]).lower())
        else:
            merged.setdefault((base + loc, "value"), []).append(str(err["msg"]))
    errors: list[ValidationError] = []
    for (path, mode), messages in merged.items():
        unique = list(dict.fromkeys(messages))
        errors.append(doc.error(path, "; ".join(unique), mode))
    return errors


def _trim_loc(loc: tuple[int | str, ...], data: Any) -> Path:
    """Keep the part of ``loc`` that addresses data; drop pydantic's union-member
    tags (e.g. ``'list[str]'``), which appear below a scalar or list value."""
    out: list[str | int] = []
    node: Any = data
    for part in loc:
        if isinstance(node, dict) and isinstance(part, str):
            mapping: dict[str, Any] = node  # pyright: ignore[reportUnknownVariableType]
            node = mapping.get(part)
        elif isinstance(node, list) and isinstance(part, int):
            items: list[Any] = node  # pyright: ignore[reportUnknownVariableType]
            node = items[part] if 0 <= part < len(items) else None
        else:
            break
        out.append(part)
    return tuple(out)
