"""Parse a tag-script file's text into a ``TagScript`` with located errors (TAGSCRIPT-1, -2).

Pure: callers read the file. ``{{ }}`` interpolation (C-WORKFLOW) applies to the
script's string values, as it does to a step's inline actions.
"""

from __future__ import annotations

from collections.abc import Callable

from rifferr.core.workflow.errors import ValidationError
from rifferr.core.workflow.loader import validate_model
from rifferr.core.workflow.template_fields import interpolate_fields
from rifferr.core.workflow.yamldoc import YamlSyntaxError, parse
from rifferr.tagscript.models import TagScript


def parse_script(
    text: str,
    source: str,
    interpolate: Callable[[str], str] | None = None,
    interpolate_template: Callable[[str], str] | None = None,
) -> tuple[TagScript | None, list[ValidationError]]:
    """The script in ``text``, or None and every problem (with line and column).
    ``interpolate_template`` handles the values that are backend templates (``set``,
    ``append_unique.values``; SEC-22), ``interpolate`` the rest."""
    try:
        doc = parse(text, source)
    except YamlSyntaxError as exc:
        return None, [exc.error]
    data = doc.plain()
    if data is None:
        return None, [ValidationError(source, "", "the script file is empty")]
    if interpolate is not None:
        data = interpolate_fields(TagScript, data, interpolate, interpolate_template or interpolate)
    script, errors = validate_model(TagScript, data, doc, ())
    return script, errors
