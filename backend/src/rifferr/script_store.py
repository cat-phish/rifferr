"""Tag-script files in a profile's ``scripts/`` folder (TAGSCRIPT-1, API-18).

<profile>/scripts/<name>.yaml

Steps name a script by its path relative to ``scripts/``; the scripts API names
files by stem, with the workflow name rules (API-3).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from rifferr.profile_store import SCRIPTS_DIR
from rifferr.tagscript.models import script_path_problem

SUFFIXES = (".yaml", ".yml")


class ScriptPathError(ValueError):
    """The path or name doesn't stay inside ``scripts/`` or names no single file."""

    def __init__(self, message: str, code: str = "invalid_script_name") -> None:
        self.code = code
        super().__init__(message)


class ScriptHashMismatch(Exception):
    """The file changed since the editor loaded it (API-18)."""


def scripts_dir(profile_dir: Path) -> Path:
    return profile_dir / SCRIPTS_DIR


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def resolve_script(profile_dir: Path, path: str) -> Path:
    """The file ``path`` names under ``scripts/``. A path that leaves the folder,
    lexically or through a symlink, is an error (TAGSCRIPT-1)."""
    problem = script_path_problem(path)
    if problem is None and "{{" in path:
        problem = "has an unresolved {{ }} expression"
    if problem:
        raise ScriptPathError(f"script {path!r}: {problem}")
    base = scripts_dir(profile_dir)
    target = base / path
    real_base = Path(os.path.realpath(base))
    real = Path(os.path.realpath(target))
    if not real.is_relative_to(real_base):
        raise ScriptPathError(f"script {path!r} leaves the profile's scripts/ folder")
    if not real.is_file():
        raise ScriptPathError(f"script {path!r} not found in {base}", "script_not_found")
    return real


@dataclass(frozen=True)
class ScriptFile:
    name: str
    path: Path
    text: str
    sha256: str


def _check_name(name: str) -> None:
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        raise ScriptPathError(f"invalid script name {name!r}")


def script_files(profile_dir: Path) -> dict[str, list[Path]]:
    """stem -> files with that stem (more than one is ambiguous)."""
    base = scripts_dir(profile_dir)
    found: dict[str, list[Path]] = {}
    if base.is_dir():
        for f in sorted(base.iterdir()):
            if f.suffix in SUFFIXES and f.is_file() and not f.name.startswith("."):
                found.setdefault(f.stem, []).append(f)
    return found


def find_script(profile_dir: Path, name: str) -> Path | None:
    """The file for ``name``; None if there is none."""
    _check_name(name)
    files = script_files(profile_dir).get(name, [])
    if len(files) > 1:
        raise ScriptPathError(
            f"script name {name!r} matches {', '.join(f.name for f in files)}",
            "ambiguous_script_name",
        )
    if not files:
        return None
    real = Path(os.path.realpath(files[0]))
    if not real.is_relative_to(Path(os.path.realpath(scripts_dir(profile_dir)))):
        raise ScriptPathError(f"script {name!r} leaves the profile's scripts/ folder")
    return files[0]


def read_script(profile_dir: Path, name: str) -> ScriptFile | None:
    path = find_script(profile_dir, name)
    if path is None:
        return None
    data = path.read_bytes()
    return ScriptFile(name, path, data.decode("utf-8"), sha256(data))


def save_script(profile_dir: Path, name: str, text: str, expected: str | None) -> ScriptFile:
    """Write ``text`` as typed. With ``expected``, the file on disk must still have
    that hash (``ScriptHashMismatch`` otherwise; a missing file has none)."""
    path = find_script(profile_dir, name)
    if expected is not None:
        current = sha256(path.read_bytes()) if path is not None else None
        if current != expected:
            raise ScriptHashMismatch(name)
    if path is None:
        base = scripts_dir(profile_dir)
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{name}.yaml"
    data = text.encode("utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return ScriptFile(name, path, text, sha256(data))
