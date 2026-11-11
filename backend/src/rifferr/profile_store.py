"""Profile directories on disk (PROFILE-1, -8, -10).

<config_root>/profiles/<name>/profile.yaml
<config_root>/profiles/<name>/workflows/
"""

from __future__ import annotations

import os
import zoneinfo
from collections.abc import Mapping
from datetime import datetime, tzinfo
from pathlib import Path

from rifferr.core.profile import (
    PROFILE_NAME_PATTERN,
    Profile,
    ProfileError,
    parse_profile,
    validate_profile_name,
)

PROFILE_FILE = "profile.yaml"
WORKFLOWS_DIR = "workflows"


def config_root(env: Mapping[str, str] | None = None) -> Path:
    """``RIFFERR_CONFIG_DIR`` (the Docker image sets ``/config``), else XDG, else ~/.config."""
    env = os.environ if env is None else env
    if env.get("RIFFERR_CONFIG_DIR"):
        return Path(env["RIFFERR_CONFIG_DIR"])
    if env.get("XDG_CONFIG_HOME"):
        return Path(env["XDG_CONFIG_HOME"]) / "rifferr"
    return Path(env.get("HOME") or Path.home()) / ".config" / "rifferr"


def profile_dir(root: Path, name: str) -> Path:
    return root / "profiles" / validate_profile_name(name)


def list_profiles(root: Path) -> list[str]:
    base = root / "profiles"
    if not base.is_dir():
        return []
    return sorted(
        d.name
        for d in base.iterdir()
        if PROFILE_NAME_PATTERN.match(d.name) and (d / PROFILE_FILE).is_file()
    )


def _timezone_exists(name: str) -> bool:
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _expand_home(value: str, env: Mapping[str, str]) -> str:
    home = env.get("HOME") or str(Path.home())
    if value == "~":
        return home
    if value.startswith("~/"):
        return home + value[1:]
    return value


def load_profile(root: Path, name: str, env: Mapping[str, str] | None = None) -> Profile:
    env = os.environ if env is None else env
    directory = profile_dir(root, name)
    path = directory / PROFILE_FILE
    if not path.is_file():
        raise ProfileError(f"no profile '{name}' (expected {path})")
    profile = parse_profile(
        path.read_text(encoding="utf-8"),
        str(path),
        name=name,
        root=directory,
        env=env,
        timezone_exists=_timezone_exists,
    )
    if profile.beets.beetsdir:
        beets = profile.beets.model_copy(
            update={"beetsdir": _expand_home(profile.beets.beetsdir, env)}
        )
        profile = profile.model_copy(update={"beets": beets})
    return profile


def create_profile(root: Path, name: str, beetsdir: Path | None = None) -> Path:
    """Create an empty profile. Never touches ``beetsdir`` beyond checking it exists."""
    directory = profile_dir(root, name)
    if directory.exists():
        raise ProfileError(f"profile '{name}' already exists at {directory}")
    if beetsdir is not None and not beetsdir.is_dir():
        raise ProfileError(f"beets directory {beetsdir} is not a directory")
    beets_line = (
        f"beets:\n  beetsdir: {_yaml_str(str(beetsdir))}\n"
        if beetsdir
        else (
            "beets:\n  # beetsdir: /path/to/beets/config/dir   # required before beets steps run\n"
            "  {}\n"
        )
    )
    text = (
        f"# Rifferr profile '{name}'. See docs: specs/core/C-PROFILE.md\n"
        "backend: beets\n"
        f"{beets_line}"
        "vars: {}\n"
        "# secrets: { name: keyring:<service>/<name> | env:<VAR> | file:/path }\n"
        "secrets: {}\n"
        "# timezone: Europe/Amsterdam   # default: system time zone\n"
    )
    (directory / WORKFLOWS_DIR).mkdir(parents=True)
    (directory / PROFILE_FILE).write_text(text, encoding="utf-8")
    return directory


def _yaml_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def effective_timezone(profile: Profile) -> tzinfo:
    """The profile's time zone, or the system's local one (PROFILE-10)."""
    if profile.timezone:
        return zoneinfo.ZoneInfo(profile.timezone)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local
