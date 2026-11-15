"""Load cover providers from the ``rifferr.cover_providers`` entry-point group
(S-COVERS-SEARCH COVERS-SEARCH-1). Built-ins register there too, in ``pyproject.toml``."""

from __future__ import annotations

import importlib.metadata
import inspect
from collections.abc import Iterable
from typing import Any

from rifferr.covers.base import CoverProvider
from rifferr.plugins import COVER_PROVIDERS
from rifferr.steps.registry import EntryPoints


def _installed(group: str) -> Iterable[Any]:
    return importlib.metadata.entry_points(group=group)


def load_providers(
    entry_points: EntryPoints = _installed,
) -> tuple[dict[str, CoverProvider], list[str]]:
    """Every installed provider by name, and the errors of broken ones (never raised)."""
    found: dict[str, CoverProvider] = {}
    errors: list[str] = []
    for ep in entry_points(COVER_PROVIDERS):
        try:
            obj = ep.load()
            provider: Any = obj() if inspect.isclass(obj) else obj
        except Exception as exc:
            errors.append(f"cover provider {ep.name} ({ep.value}): failed to load: {exc}")
            continue
        missing = [a for a in ("name", "hosts", "needs", "search") if not hasattr(provider, a)]
        if missing:
            errors.append(f"cover provider {ep.name} ({ep.value}): missing {', '.join(missing)}")
            continue
        if provider.name != ep.name:
            errors.append(f"cover provider {ep.name} ({ep.value}): its name is {provider.name!r}")
            continue
        if ep.name in found:
            errors.append(f"cover provider {ep.name} registered twice")
            continue
        found[ep.name] = provider
    return found, errors
