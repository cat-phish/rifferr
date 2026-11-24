"""beets configuration and plugin loading (BEETS-IMPORT-1, -2).

The only module that touches beets' process-wide singletons. It uses
semi-internal beets/confuse APIs, all pinned by the contract tests in
``tests/backends/beets/test_config_contract.py``:

- ``beets.config`` (a confuse ``LazyConfig``): ``clear()``, ``read()``, ``set()``
  and its ``sources`` list (to remove a temporary overlay again).
- ``beets.plugins.load_plugins()`` and ``beets.plugins._instances`` (to unload in tests).
- ``beets.util.cached_classproperty.cache`` (cleared after plugins load, as beets' own test
  helper does, so plugin field types apply).
- ``beets.plugins.get_plugin_names()``, ``find_plugins()`` and ``BeetsPlugin.name``
  (``failed_plugins``, BEETS-IMPORT-20).
- ``beets.metadata_plugins.find_metadata_source_plugins.__wrapped__`` (uncached; the backend's
  ``lookup_problem``, BEETS-IMPORT-19).
- confuse finds the config directory through the ``BEETSDIR`` environment variable.

beets reads ``$BEETSDIR/config.yaml`` and never writes it; Rifferr doesn't either.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import beets
import beets.plugins
import beets.util
import confuse

from rifferr.backends.beets import facts as album_facts
from rifferr.backends.beets import file_tags, templates
from rifferr.beets_config import ConfigTrusted, inspect_config, refusal

_loaded: Path | None = None


class BeetsConfigError(Exception):
    """The profile's beets configuration can't be used."""


def load(beetsdir: Path, trusted: ConfigTrusted | None = None, profile: str | None = None) -> None:
    """Load beets config and plugins from ``beetsdir``. Once per process (one profile).

    SEC-62: a config that runs code (``pluginpath``, ``inline``, ``hook``) loads only when
    ``trusted(sha256)`` says the user trusted exactly this content."""
    global _loaded
    beetsdir = beetsdir.expanduser().resolve()
    if _loaded == beetsdir:
        return
    if _loaded is not None:
        raise BeetsConfigError(
            f"beets is already configured from {_loaded} in this process; "
            "a worker serves exactly one profile"
        )
    if not beetsdir.is_dir():
        raise BeetsConfigError(f"beets directory {beetsdir} does not exist")
    report = inspect_config(beetsdir)
    if report.needs_trust and not (trusted is not None and trusted(report.sha256)):
        raise BeetsConfigError(refusal(beetsdir, report, profile))
    os.environ["BEETSDIR"] = str(beetsdir)
    beets.config.clear()
    try:
        beets.config.read(user=True, defaults=True)
    except confuse.ConfigError as exc:
        beets.config.clear()
        raise BeetsConfigError(f"can't read beets config in {beetsdir}: {exc}") from None
    # TEMPLATE-FUNCTIONS-1..4: the rifferr plugin always loads; collisions stop the worker.
    try:
        templates.rifferr_functions()
        file_tags.validate_config()
        album_facts.queries()
        names = beets.config["plugins"].as_str_seq()
        if templates.PLUGIN_NAME not in names:
            beets.config.set({"plugins": [*names, templates.PLUGIN_NAME]})
        beets.plugins.load_plugins()
        # Model field types come from plugins (``item_types``, FILE-TAGS-9) and beets
        # caches them per class on first use; drop what an earlier load cached.
        beets.util.cached_classproperty.cache.clear()
        templates.check_collisions()
    except (
        templates.TemplateFunctionError,
        file_tags.FileTagError,
        album_facts.FactsConfigError,
    ) as exc:
        file_tags.unregister_all()
        beets.plugins._instances.clear()  # pyright: ignore[reportPrivateUsage]
        beets.config.clear()
        raise BeetsConfigError(str(exc)) from None
    _loaded = beetsdir


def plugin_names() -> list[str]:
    """The plugins this process loaded, for the ``beet`` CLI's ``-p`` (TEMPLATE-FUNCTIONS-3)."""
    return beets.plugins.get_plugin_names()


def failed_plugins() -> list[str]:
    """Plugins the config asks for that beets couldn't load (BEETS-IMPORT-20). beets only
    logs ``** error loading plugin`` and carries on without them."""
    loaded = {plugin.name for plugin in beets.plugins.find_plugins()}
    return [name for name in beets.plugins.get_plugin_names() if name not in loaded]


def loaded_beetsdir() -> Path | None:
    return _loaded


def reset() -> None:
    """Forget the loaded config and plugins. For tests only: a real worker loads once."""
    global _loaded
    file_tags.unregister_all()
    beets.plugins._instances.clear()  # pyright: ignore[reportPrivateUsage]
    beets.config.clear()
    os.environ.pop("BEETSDIR", None)
    _loaded = None


@contextlib.contextmanager
def overlay(values: Mapping[str, Any]) -> Iterator[None]:
    """Apply ``values`` as the highest-priority config source, in memory, then remove it."""
    before = list(beets.config.sources)
    beets.config.set(dict(values))
    added = [s for s in beets.config.sources if not any(s is b for b in before)]
    try:
        yield
    finally:
        for source in added:
            beets.config.sources.remove(source)


def beets_config() -> Any:
    """beets' (overlaid) configuration, for reading settings such as ``import.timid``."""
    return beets.config


def library_paths() -> tuple[Path, Path]:
    """The configured library DB file and music directory."""
    db = Path(beets.config["library"].as_filename())
    directory = Path(beets.config["directory"].as_filename())
    return db, directory
