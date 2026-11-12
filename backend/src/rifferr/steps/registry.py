"""Load step types from the ``rifferr.steps`` entry-point group (RUN-3).

Built-in steps register there too, in ``pyproject.toml``.
"""

from __future__ import annotations

import importlib.metadata
import inspect
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from rifferr.engine import StepTypeRegistry
from rifferr.plugins import STEPS


class _EntryPoint(Protocol):
    name: str
    value: str

    def load(self) -> Any: ...


EntryPoints = Callable[[str], Iterable[_EntryPoint]]


def _installed(group: str) -> Iterable[_EntryPoint]:
    return importlib.metadata.entry_points(group=group)


def load_step_types(entry_points: EntryPoints = _installed) -> StepTypeRegistry:
    """Every installed step type. Broken plugins are listed in ``.errors``, not raised."""
    types: list[Any] = []
    errors: list[str] = []
    for ep in entry_points(STEPS):
        try:
            obj = ep.load()
            step: Any = obj() if inspect.isclass(obj) else obj
        except Exception as exc:
            errors.append(f"step type {ep.name} ({ep.value}): failed to load: {exc}")
            continue
        missing = [a for a in ("name", "params_model", "requires", "run") if not hasattr(step, a)]
        if missing:
            errors.append(f"step type {ep.name} ({ep.value}): missing {', '.join(missing)}")
            continue
        if not inspect.iscoroutinefunction(step.run):
            errors.append(f"step type {ep.name} ({ep.value}): run must be async")
            continue
        if step.name != ep.name:
            errors.append(
                f"step type {ep.name} ({ep.value}): registered as {ep.name!r} "
                f"but its name is {step.name!r}"
            )
            continue
        types.append(step)
    return StepTypeRegistry(types, errors)
