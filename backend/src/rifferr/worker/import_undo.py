"""Undo of a run's import (F-DASHBOARD DASHBOARD-7 .. -9, -18).

For each album the run imported: undo the run's applied changes to it (JOURNAL-6,
lane-scoped as LANES-11), write the removal's write-ahead record, then remove the
album through the backend. Files are deleted only when they are copies: inside the
library folders, outside the run's input folder, and the input folder still has the
fingerprint it had when the run started.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from rifferr.appdb import AppDB, AppRemovals, AppTriggers
from rifferr.backends.base import LibraryBackend
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, label
from rifferr.engine.undo import UndoContext, undo_run
from rifferr.triggers import fs


@dataclass(frozen=True)
class Originals:
    """The run's input folder and whether it is unchanged since the run started."""

    input_path: str | None
    intact: bool


def originals(recorded: tuple[str, str] | None) -> Originals:
    """``recorded``: the run's ``(input_path, fingerprint)`` (``AppTriggers.run_input``)."""
    if recorded is None:
        return Originals(None, False)
    path, fingerprint = recorded
    folder = Path(path)
    if not folder.is_dir() or _has_links(folder):  # SEC-03: links hide files from the fingerprint
        return Originals(path, False)
    return Originals(path, fs.input_fingerprint(folder) == fingerprint)


def _has_links(folder: Path) -> bool:
    for _root, dirs, names in os.walk(folder, followlinks=False):
        if any(Path(_root, n).is_symlink() for n in (*dirs, *names)):
            return True
    return False


def _digest(path: Path) -> str | None:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _are_copies(paths: Sequence[str], source: Path) -> bool:
    """SEC-03: every library file has a byte-identical file still in the input folder."""
    sizes: dict[int, list[Path]] = {}
    for root, _dirs, names in os.walk(source, followlinks=False):
        for name in names:
            full = Path(root, name)
            try:
                sizes.setdefault(full.stat().st_size, []).append(full)
            except OSError:
                continue
    for raw in paths:
        try:
            size = Path(raw).stat().st_size
        except OSError:
            return False
        digest = _digest(Path(raw))
        if digest is None or not any(_digest(c) == digest for c in sizes.get(size, ())):
            return False
    return True


def deletes_files(paths: Sequence[str], library_dirs: Sequence[Path], found: Originals) -> bool:
    """DASHBOARD-18: whether removing an album with these files deletes them."""
    if not paths or not library_dirs or not found.intact or found.input_path is None:
        return False
    source = Path(found.input_path).resolve()
    roots = [d.resolve() for d in library_dirs]
    for raw in paths:
        path = Path(raw).resolve()
        if path.is_relative_to(source):
            return False
        if not any(path.is_relative_to(root) for root in roots):
            return False
    return _are_copies(paths, source)


def library_dirs(backend: LibraryBackend) -> list[Path]:
    return backend.library_dirs() if backend.capabilities.files else []


def album_folder(album: AlbumSnapshot) -> str | None:
    if album.path:
        return album.path
    return str(Path(album.items[0].path).parent) if album.items else None


def undo_import(
    db: AppDB,
    backend: LibraryBackend,
    ctx: UndoContext,
    *,
    profile: str,
    run_id: str,
    albums: Sequence[str] | None,
) -> tuple[dict[str, JsonValue], str | None]:
    """Remove the run's albums (all, or those in ``albums``), oldest first. Returns the
    report for the job's ``result`` and the error that stopped it, if any."""
    found = backend.query(Selection(run_id=run_id))
    if albums is not None:
        wanted = set(albums)
        found = [a for a in found if a.id in wanted]
    source = originals(AppTriggers(db).run_input(run_id))
    dirs = library_dirs(backend)
    removals = AppRemovals(db)
    removed: list[JsonValue] = []
    report: dict[str, JsonValue] = {"run_id": run_id, "removed": removed, "stopped_at": None}
    for album in found:
        name = label(album)
        targets = {album.id, *(i.id for i in album.items)}
        undone = undo_run(run_id, ctx, targets=targets)
        if undone.stopped is not None:
            report["stopped_at"] = name
            report["undo"] = undone.as_json()
            return report, f"undo of {name}'s changes stopped at a conflict"
        delete = deletes_files([i.path for i in album.items], dirs, source)
        record = removals.begin(profile, run_id, album.id, name, delete)
        try:
            deleted = backend.remove_album(album.id, delete_files=delete)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            removals.finish(record.id, "failed", error=error)
            report["stopped_at"] = name
            return report, f"could not remove {name}: {error}"
        removals.finish(record.id, "removed", deleted=deleted)
        removed.append(
            {
                "album_id": album.id,
                "label": name,
                "deleted_files": len(deleted),
                "files_at": None if delete else album_folder(album),
            }
        )
    if removed and not backend.query(Selection(run_id=run_id)):
        AppTriggers(db).mark_undone(run_id)  # DASHBOARD-9
    return report, None


def recover_removals(db: AppDB, backend: LibraryBackend, profile: str) -> list[str]:
    """Settle removals a previous worker left pending: removed if the album is gone,
    failed otherwise. Never removes anything itself."""
    removals = AppRemovals(db)
    settled: list[str] = []
    for record in removals.pending(profile):
        still = backend.query(Selection(ids=(record.album_id,)))
        if still:
            removals.finish(record.id, "failed", error="interrupted before the removal")
        else:
            removals.finish(record.id, "removed")
        settled.append(record.id)
    return settled
