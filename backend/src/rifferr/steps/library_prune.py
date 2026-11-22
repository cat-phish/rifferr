"""The ``library.prune`` step (S-LIBRARY-PRUNE): drop library entries whose files are
all gone, so albums deleted by hand stop counting as duplicates.

It never deletes, moves or writes a file (LIBRARY-PRUNE-4). A path that can't be checked
counts as present, and more than ``max_albums`` gone albums stops the step before it
removes anything (LIBRARY-PRUNE-2).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.prune import Exists, all_missing, find_missing
from rifferr.core.selection import Selection
from rifferr.core.snapshot import label
from rifferr.engine import StepContext, StepResult
from rifferr.engine.prune import RemovalJournal
from rifferr.engine.run import Scalar
from rifferr.engine.steps import PruneRequest, StepFailure


class PruneParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", description="A beets query; default all albums")
    max_albums: int = Field(
        default=50,
        ge=1,
        description="Fail without removing anything when more albums than this look gone",
    )


def file_exists(library_dirs: Sequence[Path]) -> Exists:
    """``True``/``False`` for a file present or missing; ``None`` when it can't be told:
    a permission error, or the library folder holding it is missing (an unmounted drive)."""
    roots = [Path(d) for d in library_dirs]

    def exists(path: str) -> bool | None:
        try:
            os.stat(path)
        except FileNotFoundError:
            p = Path(path)
            for root in roots:
                if p.is_relative_to(root) and not root.is_dir():
                    return None
            return False
        except OSError:
            return None
        return True

    return exists


@dataclass
class Dropped:
    removed: list[str] = field(default_factory=list[str])
    skipped: list[str] = field(default_factory=list[str])
    failed: list[str] = field(default_factory=list[str])
    lines: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])


def drop_albums(
    backend: LibraryBackend,
    journal: RemovalJournal,
    run_id: str,
    album_ids: Sequence[str],
    exists: Exists,
) -> Dropped:
    """Drop each album whose files are still all gone, recording it before and after
    (LIBRARY-PRUNE-4, -6). Files are never touched."""
    out = Dropped()
    found = {a.id: a for a in backend.query(Selection(ids=tuple(album_ids)))} if album_ids else {}
    for album_id in album_ids:
        album = found.get(album_id)
        if album is None:
            out.skipped.append(album_id)
            out.lines.append(("info", f"album {album_id} is no longer in the library"))
            continue
        name = label(album)
        if not all_missing(album, exists):
            out.skipped.append(album_id)
            out.lines.append(("info", f"kept {name}: some of its files are back"))
            continue
        record = journal.begin(run_id, album, name, delete_files=False)
        try:
            backend.remove_album(album_id, delete_files=False)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            journal.finish(record, "failed", error=error)
            out.failed.append(album_id)
            out.lines.append(("error", f"could not drop {name}: {error}"))
            continue
        journal.finish(record, "removed")
        out.removed.append(album_id)
        out.lines.append(("info", f"dropped {name} from the library (its files were gone)"))
    return out


class LibraryPruneStep:
    name = "library.prune"
    params_model = PruneParams
    requires = BackendCapabilities(query=True, remove=True)
    run_level = True  # about the library, not about the run's albums
    accepts_when = False

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, PruneParams)
        backend = ctx.backend
        exists = file_exists(backend.library_dirs())
        albums = backend.query(Selection(query=params.query))
        gone, partial = find_missing(albums, exists)
        for album in partial:
            ctx.log(
                f"partial: {album.label}: {album.missing} of {album.items} files missing; "
                "left in the library",
                stream="error",
            )
        if len(gone) > params.max_albums:
            raise StepFailure(
                f"{len(gone)} albums look deleted (more than max_albums {params.max_albums}); "
                "is the library's drive mounted? Nothing was removed"
            )
        outputs: dict[str, Scalar] = {"removed": 0, "partial": len(partial), "skipped": 0}
        if not gone:
            ctx.log("no library entries without files")
            return StepResult(outputs=outputs)
        for album in gone:
            ctx.log(f"gone: {album.label} ({album.items} files)")
        if ctx.step.apply != "auto":
            decision = ctx.request_prune(PruneRequest(albums=gone, partial=partial))
            ctx.log(f"waiting for review before dropping {len(gone)} album(s)")
            return StepResult(outputs={**outputs, "decision": decision})
        dropped = drop_albums(backend, ctx.removals, ctx.run_id, [a.album_id for a in gone], exists)
        for stream, line in dropped.lines:
            ctx.log(line, stream=stream)
        if dropped.failed:
            raise StepFailure(f"{len(dropped.failed)} album(s) couldn't be dropped; see the log")
        outputs["removed"] = len(dropped.removed)
        outputs["skipped"] = len(dropped.skipped)
        return StepResult(outputs=outputs)
