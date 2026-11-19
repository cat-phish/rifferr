"""Does a profile's beets config run code? (SEC-62)

beets loads Python from ``pluginpath`` folders, evaluates ``inline`` fields and runs
``hook`` commands. A config the user did not write is therefore code. This module reads
the config as plain YAML (no beets import, so the API server can use it), lists the keys
that run code and hashes the config files. The hash is what a user trusts, per profile,
in ``trusted_files`` (the same store as workflow trust).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

CODE_PLUGINS = frozenset({"inline", "hook"})
"""Plugins that run user-written code or commands."""

_MAX_INCLUDES = 20
_HASH_PREFIX = b"rifferr beets-config v1\0"  # keeps this hash apart from workflow hashes


@dataclass(frozen=True)
class ConfigReport:
    sha256: str
    files: list[str] = field(default_factory=list)
    code_keys: list[str] = field(default_factory=list)
    """Human-readable lines, e.g. ``config.yaml: pluginpath: /x/plugins``."""
    unreadable: list[str] = field(default_factory=list)
    plugins: list[str] = field(default_factory=list)
    """Every plugin the config files enable, in order (S-LIBRARY-MOVE LIBRARY-MOVE-6a)."""

    @property
    def needs_trust(self) -> bool:
        """Code keys found. A file that can't be read or parsed can't load in beets either,
        so beets reports that itself."""
        return bool(self.code_keys)


def _plugins(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def inspect_config(beetsdir: Path) -> ConfigReport:
    """Read ``config.yaml`` and the ``include:`` files it names (relative to the beets
    folder, as beets does), never importing or running anything."""
    beetsdir = beetsdir.expanduser().resolve()
    digest = hashlib.sha256(_HASH_PREFIX)
    files: list[str] = []
    keys: list[str] = []
    bad: list[str] = []
    plugins: list[str] = []
    queue = [beetsdir / "config.yaml"]
    seen: set[Path] = set()
    yaml = YAML(typ="safe")
    while queue and len(seen) < _MAX_INCLUDES:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        name = str(path.relative_to(beetsdir)) if path.is_relative_to(beetsdir) else str(path)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            if path == beetsdir / "config.yaml":
                continue
            bad.append(f"{name}: missing")
            continue
        except OSError as exc:
            bad.append(f"{name}: {exc.strerror}")
            continue
        digest.update(name.encode() + b"\0" + data + b"\0")
        files.append(name)
        try:
            doc = yaml.load(data)
        except YAMLError:
            bad.append(f"{name}: not valid YAML")
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("pluginpath"):
            keys.append(f"{name}: pluginpath: {doc['pluginpath']}")
        plugins += [p for p in _plugins(doc.get("plugins")) if p not in plugins]
        used = sorted(CODE_PLUGINS.intersection(_plugins(doc.get("plugins"))))
        if used:
            keys.append(f"{name}: plugins: {', '.join(used)}")
        for inc in _plugins(doc.get("include")):
            queue.append((beetsdir / inc).resolve())
    if queue:
        bad.append("too many include files")
    return ConfigReport(digest.hexdigest(), files, keys, bad, plugins)


def enabled_plugins(beetsdir: str | Path | None) -> list[str]:
    """The plugins a beets config enables, or none without a beets folder."""
    return inspect_config(Path(beetsdir)).plugins if beetsdir else []


ConfigTrusted = Callable[[str], bool]
"""``sha256 -> whether the user trusted this beets config in the profile``."""


def refusal(beetsdir: Path, report: ConfigReport, profile: str | None = None) -> str:
    found = "; ".join(report.code_keys)
    cmd = f"rifferr trust-config {profile}" if profile else "rifferr trust-config PROFILE"
    return (
        f"the beets config in {beetsdir} is not trusted and it runs code ({found}). "
        f"Read it, then run `{cmd}` to trust exactly this content"
    )
