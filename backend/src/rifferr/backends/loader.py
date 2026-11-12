"""Load a profile's backend by name from the ``rifferr.backends`` entry points.

An entry point names a factory ``(profile: Profile, *, read_only: bool = False)
-> LibraryBackend``. Read-only, the backend refuses every write (BACKEND-13).
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from typing import Any

from rifferr.backends.base import LibraryBackend
from rifferr.core.profile import Profile, ProfileError
from rifferr.plugins import BACKENDS

BackendFactory = Callable[..., LibraryBackend]


def load_backend(
    profile: Profile,
    entry_points: Callable[[str], Iterable[Any]] = lambda g: importlib.metadata.entry_points(
        group=g
    ),
    *,
    read_only: bool = False,
) -> LibraryBackend:
    found = {ep.name: ep for ep in entry_points(BACKENDS)}
    ep = found.get(profile.backend)
    if ep is None:
        known = ", ".join(sorted(found)) or "none installed"
        raise ProfileError(
            f"profile '{profile.name}' uses backend '{profile.backend}', which is not "
            f"installed (known: {known})"
        )
    factory: BackendFactory = ep.load()
    if not read_only:
        return factory(profile)
    backend = factory(profile, read_only=True)
    if backend.capabilities.apply or backend.capabilities.import_:
        raise ProfileError(f"backend '{profile.backend}' didn't open read-only")
    return backend
