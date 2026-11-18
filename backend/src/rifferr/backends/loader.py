"""Load a profile's backend by name from the ``rifferr.backends`` entry points.

An entry point names a factory ``(profile, *, read_only=False, config_trusted=None)
-> LibraryBackend``. ``config_trusted(sha256)`` says whether the user trusted the
backend's own config content in this profile (SEC-62). Read-only, the backend refuses
every write (BACKEND-13).
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
    config_trusted: Callable[[str], bool] | None = None,
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
    if config_trusted is None:  # SEC-62: trust the user gave this profile's backend config
        from rifferr.profile_store import app_db_path
        from rifferr.trust import config_trust_checker

        config_trusted = config_trust_checker(app_db_path(), profile.name)
    if not read_only:
        return factory(profile, config_trusted=config_trusted)
    backend = factory(profile, read_only=True, config_trusted=config_trusted)
    if backend.capabilities.apply or backend.capabilities.import_:
        raise ProfileError(f"backend '{profile.backend}' didn't open read-only")
    return backend
