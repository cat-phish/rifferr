"""Load AI providers and AI tasks from their entry point groups (AI-CORE-1, -8).

Broken plugins are reported, never raised, like steps and cover providers.
"""

from __future__ import annotations

import importlib.metadata
import inspect
from collections.abc import Iterable
from typing import Any

from rifferr.ai.base import AIProvider, AITask
from rifferr.plugins import AI_PROVIDERS, AI_TASKS
from rifferr.steps.registry import EntryPoints


def _installed(group: str) -> Iterable[Any]:
    return importlib.metadata.entry_points(group=group)


def _load(
    group: str, what: str, key: str, attrs: tuple[str, ...], entry_points: EntryPoints
) -> tuple[dict[str, Any], list[str]]:
    found: dict[str, Any] = {}
    errors: list[str] = []
    for ep in entry_points(group):
        try:
            obj = ep.load()
            plugin: Any = obj() if inspect.isclass(obj) else obj
        except Exception as exc:
            errors.append(f"{what} {ep.name} ({ep.value}): failed to load: {exc}")
            continue
        missing = [a for a in attrs if not hasattr(plugin, a)]
        if missing:
            errors.append(f"{what} {ep.name} ({ep.value}): missing {', '.join(missing)}")
            continue
        name = getattr(plugin, key)
        if name != ep.name:
            errors.append(f"{what} {ep.name} ({ep.value}): its {key} is {name!r}")
            continue
        if ep.name in found:
            errors.append(f"{what} {ep.name} registered twice")
            continue
        found[ep.name] = plugin
    return found, errors


def load_providers(
    entry_points: EntryPoints = _installed,
) -> tuple[dict[str, AIProvider], list[str]]:
    """Every installed provider by type (AI-CORE-1)."""
    attrs = ("type", "default_host", "default_base_url", "complete")
    return _load(AI_PROVIDERS, "AI provider", "type", attrs, entry_points)


def load_tasks(
    entry_points: EntryPoints = _installed,
) -> tuple[dict[str, AITask[Any, Any]], list[str]]:
    """Every installed AI task by id (AI-CORE-8)."""
    attrs = ("id", "prompt_version", "input_model", "output_model", "sends", "files", "build",
             "validate")  # fmt: skip
    return _load(AI_TASKS, "AI task", "id", attrs, entry_points)
