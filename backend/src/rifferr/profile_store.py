"""Profile directories on disk (PROFILE-1, -8, -10).

<config_root>/profiles/<name>/profile.yaml
<config_root>/profiles/<name>/workflows/
<config_root>/profiles/<name>/scripts/
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zoneinfo
from collections.abc import Mapping, Sequence
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from rifferr.core.profile import (
    PROFILE_NAME_PATTERN,
    Profile,
    ProfileError,
    ProfileLoadError,
    parse_profile,
    validate_profile_name,
)

PROFILE_FILE = "profile.yaml"
WORKFLOWS_DIR = "workflows"
SCRIPTS_DIR = "scripts"


APP_DB_FILE = "rifferr.db"


def atomic_write(path: Path, data: bytes) -> None:
    """SEC-09: write via a fresh unique temp file (never a pre-existing name or link)."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            mode = path.stat().st_mode & 0o777
        except OSError:
            mode = 0o644
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """Where the app DB lives: ``RIFFERR_DATA_DIR``, else ``RIFFERR_CONFIG_DIR`` (Docker keeps
    everything in ``/config``), else ``$XDG_DATA_HOME/rifferr``, else ~/.local/share/rifferr."""
    env = os.environ if env is None else env
    if env.get("RIFFERR_DATA_DIR"):
        return Path(env["RIFFERR_DATA_DIR"])
    if env.get("RIFFERR_CONFIG_DIR"):
        return Path(env["RIFFERR_CONFIG_DIR"])
    if env.get("XDG_DATA_HOME"):
        return Path(env["XDG_DATA_HOME"]) / "rifferr"
    return Path(env.get("HOME") or Path.home()) / ".local" / "share" / "rifferr"


def app_db_path(env: Mapping[str, str] | None = None) -> Path:
    return data_root(env) / APP_DB_FILE


def art_store_path(profile: str, env: Mapping[str, str] | None = None) -> Path:
    """The profile's art store (S-COVERS-SEARCH COVERS-SEARCH-16)."""
    return data_root(env) / "art" / validate_profile_name(profile)


def covers_cache_path(env: Mapping[str, str] | None = None) -> Path:
    """The cover search cache (COVERS-SEARCH-11)."""
    return data_root(env) / "cache" / "covers"


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
        "# worker: { idle_timeout: 600 }  # seconds without a run; null = stay up\n"
    )
    (directory / WORKFLOWS_DIR).mkdir(parents=True)
    (directory / SCRIPTS_DIR).mkdir()  # tag scripts (S-TAGSCRIPT)
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


# -- cover provider defaults (F-COVER-PICKER COVER-PICKER-10) ----------------------------


class ProfileChanged(Exception):
    """``profile.yaml`` changed since the caller read it."""


def profile_file_hash(root: Path, name: str) -> str:
    """sha256 of ``profile.yaml`` as it is on disk."""
    path = profile_dir(root, name) / PROFILE_FILE
    if not path.is_file():
        raise ProfileError(f"no profile '{name}' (expected {path})")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_cover_providers(
    root: Path,
    name: str,
    providers: Sequence[tuple[str, bool, float]],
    expected_hash: str,
    env: Mapping[str, str] | None = None,
) -> str:
    """Rewrite ``covers.providers`` in ``profile.yaml`` in the given order with each
    provider's ``enabled`` and ``weight`` (name, enabled, weight), keeping everything else:
    other keys, comments, each provider's own ``settings``, and listed providers that
    aren't in ``providers`` (after the others). The file must still have
    ``expected_hash`` (``ProfileChanged``) and the result must load as a profile
    (``ProfileLoadError``). Returns the new hash."""
    from ruamel.yaml.comments import CommentedMap, CommentedSeq

    from rifferr.core.workflow.yamldoc import YamlSyntaxError, parse

    path = profile_dir(root, name) / PROFILE_FILE
    if not path.is_file():
        raise ProfileError(f"no profile '{name}' (expected {path})")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ProfileChanged(name)
    try:
        doc = parse(raw.decode("utf-8"), str(path))
    except YamlSyntaxError as exc:
        raise ProfileLoadError([exc.error]) from None
    if not isinstance(doc.data, CommentedMap):
        doc.data = CommentedMap()
    data: Any = doc.data
    covers: Any = data.get("covers")
    if not isinstance(covers, CommentedMap):
        covers = CommentedMap()
        data["covers"] = covers
    old: Any = covers.get("providers")
    entries: list[Any] = list(old) if isinstance(old, CommentedSeq) else []
    by_name = {e.get("name"): e for e in entries if isinstance(e, CommentedMap)}
    seq = CommentedSeq()
    for provider, enabled, weight in providers:
        entry = by_name.pop(provider, None)
        if entry is None:
            entry = CommentedMap()
            entry["name"] = provider
        entry["enabled"] = enabled
        entry["weight"] = int(weight) if float(weight).is_integer() else float(weight)
        seq.append(entry)
    for entry in entries:
        if isinstance(entry, CommentedMap) and entry.get("name") in by_name:
            seq.append(entry)
    covers["providers"] = seq
    doc.set(("covers",), covers)
    text = doc.dump()
    env = os.environ if env is None else env
    parse_profile(
        text, str(path), name=name, root=path.parent, env=env, timezone_exists=_timezone_exists
    )
    data_out = text.encode("utf-8")
    atomic_write(path, data_out)
    return hashlib.sha256(data_out).hexdigest()
