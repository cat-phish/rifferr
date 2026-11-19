"""Verified file copies and moves on the local filesystem (S-LIBRARY-MOVE).

Shared by backends that keep music in ordinary files. Never overwrites: the final name
is created with ``link``, which fails if it exists. Never removes a source before the
destination is verified against the journaled size and SHA-256 (LIBRARY-MOVE-8).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import posixpath
import shutil

from rifferr.backends.base import FileMoveError
from rifferr.core.changeset import FileEffect

CHUNK = 1 << 20
#: Temporary copies are hidden files beside the destination, removed by recovery.
TMP_SUFFIX = ".rifferr-part"


def stat_hash(path: str) -> tuple[int, str] | None:
    """(size, sha256) of a regular file, or None."""
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def sha256(path: str) -> str | None:
    info = stat_hash(path)
    return None if info is None else info[1]


def list_files(folder: str) -> list[str]:
    """Regular files below ``folder``, relative, ``/``-separated. Symlinks are skipped."""
    out: list[str] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in files:
            full = os.path.join(root, name)
            if os.path.islink(full) or not os.path.isfile(full) or name.endswith(TMP_SUFFIX):
                continue
            out.append(os.path.relpath(full, folder).replace(os.sep, "/"))
    return out


def tmp_path(dst: str) -> str:
    head, name = posixpath.split(dst)
    return posixpath.join(head, f".{name}{TMP_SUFFIX}")


def _verified(path: str, effect: FileEffect) -> bool:
    return stat_hash(path) == (effect.size, effect.sha256)


def _fsync_dir(folder: str) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def copy_file(effect: FileEffect) -> FileEffect:
    """``planned`` -> ``copied`` (or ``done`` for a same-filesystem move)."""
    src, dst = effect.src, effect.dst
    if os.path.lexists(dst):
        raise FileMoveError(f"{dst} already exists")
    if not _verified(src, effect):
        raise FileMoveError(f"{src} is missing or changed since it was planned")
    folder = posixpath.dirname(dst)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise FileMoveError(f"can't create {folder}: {exc.strerror}") from exc
    if effect.mode == "move":
        try:
            os.link(src, dst)  # same filesystem: atomic, and never overwrites
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise FileMoveError(f"{dst} already exists") from exc
        else:
            _fsync_dir(folder)
            os.unlink(src)
            _fsync_dir(posixpath.dirname(src))
            return effect.model_copy(update={"state": "done"})
    tmp = tmp_path(dst)
    try:
        with open(src, "rb") as fi, open(tmp, "xb") as fo:
            shutil.copyfileobj(fi, fo, CHUNK)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, tmp)
        if not _verified(tmp, effect):
            raise FileMoveError(f"the copy of {src} doesn't match it")
        os.link(tmp, dst)  # fails if dst appeared meanwhile: no overwrite
        os.unlink(tmp)
        _fsync_dir(folder)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        if exc.errno == errno.EEXIST:
            raise FileMoveError(f"{dst} already exists") from exc
        raise FileMoveError(f"copying {src}: {exc.strerror or exc}") from exc
    except FileMoveError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return effect.model_copy(update={"state": "copied"})


def remove_source(effect: FileEffect) -> FileEffect:
    """``copied`` -> ``done``: removes ``src`` only once ``dst`` is verified."""
    if not _verified(effect.dst, effect):
        raise FileMoveError(f"{effect.dst} is missing or doesn't match; {effect.src} is kept")
    try:
        os.unlink(effect.src)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise FileMoveError(f"removing {effect.src}: {exc.strerror}") from exc
    _fsync_dir(posixpath.dirname(effect.src))
    return effect.model_copy(update={"state": "done"})


def reconcile_file(effect: FileEffect) -> FileEffect:
    """A file's state after a crash, from the filesystem (LIBRARY-MOVE-12)."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp_path(effect.dst))
    if not _verified(effect.dst, effect):
        return effect.model_copy(update={"state": "planned"})
    state = "copied" if os.path.lexists(effect.src) else "done"
    return effect.model_copy(update={"state": state})
