"""Which step parameters are backend templates (SEC-22).

A parameter whose ``Field`` carries ``json_schema_extra=TEMPLATE_EXTRA`` is evaluated by
the library backend as a template (beets: ``$field``, ``%func{}``) after ``{{ }}``
interpolation. Values substituted into it from outside the workflow must be escaped
first, or the second evaluator would parse them: ``interpolate_fields`` walks a params
model and uses one interpolator for template leaves and another for the rest, so the
two syntaxes never meet in one evaluator.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Annotated, Any, Final, Union, cast, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

TEMPLATE_KEY: Final = "x-rifferr-template"
TEMPLATE_EXTRA: Final[dict[str, Any]] = {TEMPLATE_KEY: True}

Interpolate = Callable[[str], str]


def is_template_field(field: FieldInfo) -> bool:
    extra = cast(Any, field.json_schema_extra)
    return isinstance(extra, dict) and bool(cast(dict[str, Any], extra).get(TEMPLATE_KEY))


def interpolate_fields(
    model: type[BaseModel], value: Any, plain: Interpolate, template: Interpolate
) -> Any:
    """``value`` (raw ``with:`` data for ``model``) with every string interpolated:
    by ``template`` inside template fields, by ``plain`` everywhere else. Dict keys
    are never interpolated."""
    return _walk([model], value, plain, template)


def _expand(tp: Any) -> list[Any]:
    origin = get_origin(tp)
    if origin is Annotated:
        return _expand(get_args(tp)[0])
    if origin is Union or origin is types.UnionType:
        return [t for arg in get_args(tp) for t in _expand(arg)]
    return [tp]


def _field(model: type[BaseModel], key: str) -> FieldInfo | None:
    for name, field in model.model_fields.items():
        if key in (name, field.alias, field.validation_alias):
            return field
    return None


def _walk(types_: list[Any], value: Any, plain: Interpolate, template: Interpolate) -> Any:
    flat = [t for tp in types_ for t in _expand(tp)]
    if isinstance(value, str):
        return plain(value)
    if isinstance(value, dict):
        data = cast(dict[str, Any], value)
        models = [t for t in flat if isinstance(t, type) and issubclass(t, BaseModel)]
        if models:
            out: dict[str, Any] = {}
            for key, item in data.items():
                fields = [f for m in models if (f := _field(m, key)) is not None]
                if any(is_template_field(f) for f in fields):
                    out[key] = _map(item, template)
                else:
                    out[key] = _walk([f.annotation for f in fields], item, plain, template)
            return out
        inner = [get_args(t)[1] for t in flat if get_origin(t) is dict and len(get_args(t)) == 2]
        return {k: _walk(inner, v, plain, template) for k, v in data.items()}
    if isinstance(value, list):
        inner = [get_args(t)[0] for t in flat if get_origin(t) is list and get_args(t)]
        return [_walk(inner, v, plain, template) for v in cast(list[Any], value)]
    return value


def _map(value: Any, fn: Interpolate) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, dict):
        return {k: _map(v, fn) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_map(v, fn) for v in cast(list[Any], value)]
    return value
