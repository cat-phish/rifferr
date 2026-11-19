"""Rifferr template functions inside beets (C-TEMPLATE-FUNCTIONS).

Functions come from the ``rifferr.template_functions`` entry points and reach beets
through one beets plugin, ``rifferr`` (TEMPLATE-FUNCTIONS-2). beets finds plugins by
importing ``beetsplug.<name>``, so the plugin class lives in the small
``beetsplug/rifferr`` shim this package ships; it only subclasses
:class:`RifferrTemplatePlugin`.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
from collections.abc import Callable, Iterator
from importlib.metadata import entry_points

from beets.library.models import DefaultTemplateFunctions
from beets.plugins import BeetsPlugin, find_plugins

from rifferr.plugins import TEMPLATE_FUNCTIONS

#: The beets plugin name: ``plugins: [rifferr]``.
PLUGIN_NAME = "rifferr"

TemplateFunc = Callable[..., str]


class TemplateFunctionError(Exception):
    """A template function is broken, collides with another, or raised."""


_cache: dict[str, TemplateFunc] | None = None
_failures: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "rifferr_template_failures", default=None
)


@contextlib.contextmanager
def capture() -> Iterator[list[str]]:
    """Collect template-function failures during one evaluation.

    beets catches exceptions from template functions and inlines ``<message>`` into the
    result; callers check this list instead and raise a template error (-5)."""
    errors: list[str] = []
    token = _failures.set(errors)
    try:
        yield errors
    finally:
        _failures.reset(token)


def _wrap(name: str, func: Callable[..., object]) -> TemplateFunc:
    @functools.wraps(func)
    def call(*args: str) -> str:
        try:
            return str(func(*args))
        except Exception as exc:  # TEMPLATE-FUNCTIONS-5: name the function
            message = f"template function %{name} failed: {exc}"
            errors = _failures.get()
            if errors is not None:
                errors.append(message)
            raise TemplateFunctionError(message) from exc

    return call


def rifferr_functions() -> dict[str, TemplateFunc]:
    """Every installed ``rifferr.template_functions`` entry point, wrapped (-1, -4)."""
    global _cache
    if _cache is not None:
        return _cache
    owners: dict[str, str] = {}
    eps = list(entry_points(group=TEMPLATE_FUNCTIONS))
    for ep in eps:
        owner = ep.dist.name if ep.dist is not None else ep.value
        if ep.name in owners and owners[ep.name] != owner:
            raise TemplateFunctionError(
                f"template function %{ep.name} is provided by both {owners[ep.name]} and {owner}"
            )
        owners[ep.name] = owner
    out: dict[str, TemplateFunc] = {}
    for ep in eps:
        try:
            func = ep.load()
        except Exception as exc:
            raise TemplateFunctionError(
                f"template function %{ep.name} ({ep.value}) can't be loaded: {exc}"
            ) from exc
        if not callable(func):
            raise TemplateFunctionError(
                f"template function %{ep.name} ({ep.value}) is not callable"
            )
        out[ep.name] = _wrap(ep.name, func)
    _cache = out
    return out


def check_collisions() -> None:
    """After plugins loaded: no name defined twice across beets and plugins (-4)."""
    owners = {
        n[len("tmpl_") :]: "beets" for n in dir(DefaultTemplateFunctions) if n.startswith("tmpl_")
    }
    for plugin in find_plugins():
        for name in plugin.template_funcs:
            if name in owners:
                raise TemplateFunctionError(
                    f"template function %{name} is provided by both "
                    f"{owners[name]} and the beets plugin {plugin.name}"
                )
            owners[name] = f"the beets plugin {plugin.name}"


class RifferrTemplatePlugin(BeetsPlugin):
    """Registers Rifferr template functions with beets (BeetsPlugin.template_funcs)."""

    def __init__(self) -> None:
        super().__init__(PLUGIN_NAME)
        self.template_funcs.update(rifferr_functions())
