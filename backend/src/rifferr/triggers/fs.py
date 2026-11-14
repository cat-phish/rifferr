"""Filesystem parts of F-TRIGGERS: input listings and the ``within`` check."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from rifferr.core.triggers import fingerprint


def files_under(folder: Path) -> Iterator[tuple[str, int, int]]:
    """``(relative path, size, mtime_ns)`` of every regular file under ``folder``.

    Symlinks are not followed; entries that vanish or can't be read are left out.
    """
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs.sort()
        for name in names:
            full = os.path.join(root, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if os.path.isfile(full) and not os.path.islink(full):
                yield os.path.relpath(full, folder), st.st_size, st.st_mtime_ns


def input_fingerprint(folder: Path) -> str | None:
    """TRIGGERS-19: ``None`` for a folder without files (never deduplicated)."""
    return fingerprint(files_under(folder))


def has_files(folder: Path) -> bool:
    return next(files_under(folder), None) is not None


class OutsideError(ValueError):
    """A webhook input that isn't an existing folder inside ``within`` (TRIGGERS-16)."""


def confine(candidate: str, within: Path) -> Path:
    """The folder a webhook input names: the path itself, or a file's parent folder,
    resolved (symlinks too) and required to lie inside ``within``."""
    path = Path(candidate)
    if not path.is_absolute():
        raise OutsideError("input must be an absolute path")
    try:
        real = path.resolve(strict=True)
        root = within.resolve(strict=True)
    except OSError:
        raise OutsideError("input does not exist") from None
    if real.is_file():
        real = real.parent
    if not real.is_dir():
        raise OutsideError("input is not a folder")
    if real == root or not real.is_relative_to(root):
        raise OutsideError("input is not inside the webhook's 'within' folder")
    return real
