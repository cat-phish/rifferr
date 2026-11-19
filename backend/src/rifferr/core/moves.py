"""Library move plans (S-LIBRARY-MOVE): path formats, artifact rules, conflict rules.

Pure. Backends compute destinations and read the filesystem; the rules for which
untracked files travel and what counts as a conflict live here so every backend
applies them the same way.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Iterable
from datetime import datetime
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rifferr.core.changeset import FileEffect, FileKind, FileMode, Stamp, TargetRef

_FROZEN = ConfigDict(frozen=True, extra="forbid")

__all__ = ["Stamp"]  # re-exported: steps import it from here


class PathFormat(BaseModel):
    """One ``paths:`` entry: a backend query (or ``default``) and a path format."""

    model_config = _FROZEN

    query: str = Field(min_length=1)
    format: str = Field(min_length=1)


class ArtifactRule(BaseModel):
    """Which untracked files travel with the album, and where (LIBRARY-MOVE-1, -6)."""

    model_config = _FROZEN

    match: list[str] = Field(min_length=1)
    """Globs, matched case-insensitively against the path relative to the album folder."""
    to: str | None = None
    """Sub-folder of the destination album folder; the file keeps only its name."""

    @model_validator(mode="after")
    def _inside(self) -> Self:
        if self.to is not None:
            parts = PurePosixPath(self.to.replace("\\", "/")).parts
            if not parts or self.to.startswith("/") or ".." in parts:
                raise ValueError(f"'to' must be a relative folder inside the album: {self.to!r}")
        return self

    def matches(self, rel: str) -> bool:
        low = rel.lower()
        return any(fnmatchcase(low, glob.lower()) for glob in self.match)


#: The default: every untracked file, at its current relative path.
ALL_ARTIFACTS = (ArtifactRule(match=["*"]),)


def artifact_destination(rel: str, rules: Iterable[ArtifactRule]) -> str | None:
    """Where ``rel`` (relative to the album folder, ``/``-separated) goes, relative to the
    destination album folder; ``None`` if no rule matches (it stays behind). First match wins."""
    for rule in rules:
        if rule.matches(rel):
            return rel if rule.to is None else posixpath.join(rule.to, posixpath.basename(rel))
    return None


class MovePlan(BaseModel):
    """One album's planned file effects (LIBRARY-MOVE-3)."""

    model_config = _FROZEN

    album: TargetRef
    label: str
    src_dir: str
    dst_dir: str
    files: list[FileEffect] = Field(default_factory=list[FileEffect])
    conflicts: list[str] = Field(default_factory=list[str])
    """Why the album can't move (LIBRARY-MOVE-5); empty when it can."""


class MoveSettings(BaseModel):
    """A ``library.move`` step's resolved ``with:``, kept so a conflict can be retried
    the same way (LIBRARY-MOVE-5a). ``directory`` is absolute, or ``None`` for beets'."""

    model_config = _FROZEN

    mode: FileMode = "move"
    formats: list[PathFormat] | None = None
    directory: str | None = None
    artifacts: list[ArtifactRule] = Field(default_factory=lambda: list(ALL_ARTIFACTS))
    stamp: Stamp | None = None


MoveConflictState = Literal["pending", "moved", "in_place", "failed", "cancelled"]
MoveChoice = Literal["retry", "fail"]


class MoveConflict(BaseModel):
    """An album that can't move yet, waiting in the review queue (LIBRARY-MOVE-5a)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    profile: str
    run_id: str
    step_id: str
    album_id: str
    label: str
    state: MoveConflictState = "pending"
    conflicts: list[str]
    src_dir: str = ""
    dst_dir: str = ""
    settings: MoveSettings
    created_at: datetime
    attempts: int = Field(default=0, description="Retries that still found conflicts")
    decided_by: str | None = None
    decided_at: datetime | None = None
    changeset_id: str | None = Field(default=None, description="The FileChangeSet a retry applied")

    def hold_key(self) -> str:
        return self.album_id


def album_folder(paths: Iterable[str]) -> str:
    """The deepest folder holding every one of ``paths`` (files)."""
    parents = [posixpath.dirname(p) for p in paths]
    if not parents:
        raise ValueError("no files")
    return posixpath.commonpath(parents) if len(parents) > 1 else parents[0]


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def check_conflicts(
    plans: list[MovePlan],
    directory: str,
    *,
    exists: Callable[[str], bool],
    is_dir: Callable[[str], bool],
) -> list[MovePlan]:
    """``plans`` with LIBRARY-MOVE-5 conflicts filled in. Destinations must already be
    resolved (``..`` and symlinks) by the backend; ``exists``/``is_dir`` only read."""
    seen: dict[str, str] = {}
    out: list[MovePlan] = []
    for plan in plans:
        problems: list[str] = []
        if plan.dst_dir != plan.src_dir and is_dir(plan.dst_dir):
            problems.append(
                f"destination folder {plan.dst_dir} already exists (it isn't this album's folder)"
            )
        for f in plan.files:
            if not inside(f.dst, directory):
                problems.append(f"{f.src}: destination {f.dst} is outside {directory}")
            if f.dst in seen and seen[f.dst] != f.src:
                problems.append(f"{f.src} and {seen[f.dst]} both go to {f.dst}")
            seen.setdefault(f.dst, f.src)
            # Even another file of this album: moving onto it would overwrite it.
            if f.dst != f.src and exists(f.dst):
                problems.append(f"{f.src}: {f.dst} already exists")
        out.append(
            plan.model_copy(update={"conflicts": [*plan.conflicts, *problems]})
            if problems
            else plan
        )
    return out


#: One file the library tracks: (target, kind, current path, destination).
Tracked = tuple[TargetRef, FileKind, str, str]
#: (size, sha256) of an existing file, or None.
Stat = Callable[[str], tuple[int, str] | None]


def build_plan(
    album: TargetRef,
    label: str,
    tracked: list[Tracked],
    *,
    mode: FileMode,
    artifacts: Iterable[ArtifactRule],
    list_files: Callable[[str], list[str]],
    stat: Stat,
) -> MovePlan:
    """One album's plan (LIBRARY-MOVE-3, -6): tracked files whose destination differs, then
    untracked files under the album folder that an artifact rule sends along.

    ``list_files(folder)`` lists files below ``folder`` recursively, relative, with ``/``.
    """
    rules = list(artifacts)
    items = [(src, dst) for _, kind, src, dst in tracked if kind == "item"]
    src_dir = album_folder(s for s, _ in items)
    dst_dir = album_folder(d for _, d in items)
    files: list[FileEffect] = []
    problems: list[str] = []
    known = {src for _, _, src, _ in tracked}

    def add(target: TargetRef, kind: FileKind, src: str, dst: str) -> None:
        info = stat(src)
        if info is None:
            problems.append(f"{src} is missing")
            return
        size, sha = info
        files.append(
            FileEffect(target=target, kind=kind, src=src, dst=dst, mode=mode, size=size, sha256=sha)
        )

    for target, kind, src, dst in tracked:
        if dst != src:
            add(target, kind, src, dst)
    if rules:
        for rel in sorted(list_files(src_dir)):
            src = posixpath.join(src_dir, rel)
            if src in known:
                continue
            to = artifact_destination(rel, rules)
            if to is None:
                continue  # stays behind for files.cleanup (LIBRARY-MOVE-6)
            dst = posixpath.normpath(posixpath.join(dst_dir, to))
            if not inside(dst, dst_dir):
                problems.append(f"{src}: artifact destination {dst} leaves the album folder")
            elif dst != src:
                add(album, "artifact", src, dst)
    return MovePlan(
        album=album, label=label, src_dir=src_dir, dst_dir=dst_dir, files=files, conflicts=problems
    )


def shared_folders(plans: list[MovePlan]) -> list[MovePlan]:
    """Albums whose folder holds another album's folder can't tell their artifacts apart:
    a conflict instead of guessing (LIBRARY-MOVE-5, -6)."""
    out: list[MovePlan] = []
    for plan in plans:
        others = [o.label for o in plans if o is not plan and inside(o.src_dir, plan.src_dir)]
        if others and any(f.kind == "artifact" for f in plan.files):
            note = (
                f"folder {plan.src_dir} also holds {', '.join(others)}; "
                "its other files can't be told apart"
            )
            plan = plan.model_copy(update={"conflicts": [*plan.conflicts, note]})
        out.append(plan)
    return out
