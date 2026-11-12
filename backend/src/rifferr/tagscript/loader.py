"""Parse a tag-script file's text into a ``TagScript`` with located errors (TAGSCRIPT-1, -2).

Pure: callers read the file. ``{{ }}`` interpolation (C-WORKFLOW) applies to the
script's string values, as it does to a step's inline actions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from rifferr.core.workflow.errors import ValidationError
from rifferr.core.workflow.loader import validate_model
from rifferr.core.workflow.yamldoc import YamlSyntaxError, parse
from rifferr.tagscript.models import TagScript


def parse_script(
    text: str, source: str, interpolate: Callable[[str], str] | None = None
) -> tuple[TagScript | None, list[ValidationError]]:
    """The script in ``text``, or None and every problem (with line and column)."""
    try:
        doc = parse(text, source)
    except YamlSyntaxError as exc:
        return None, [exc.error]
    data = doc.plain()
    if data is None:
        return None, [ValidationError(source, "", "the script file is empty")]
    if interpolate is not None:
        data = _interpolate(data, interpolate)
    script, errors = validate_model(TagScript, data, doc, ())
    return script, errors


def _interpolate(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        mapping = cast(dict[str, Any], value)
        return {k: _interpolate(v, fn) for k, v in mapping.items()}
    if isinstance(value, list):
        return [_interpolate(v, fn) for v in cast(list[Any], value)]
    return value
