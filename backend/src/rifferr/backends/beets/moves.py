"""Move planning and library path updates for BeetsBackend (S-LIBRARY-MOVE).

Destinations come from beets itself: ``Item.destination()`` with the profile's ``paths:``
(or the step's ``paths``), ``replace:`` rules and template functions, and
``Album.art_destination()`` for the cover (LIBRARY-MOVE-3). File copies live in
``rifferr.backends.localfs``.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from beets.library import Album, Item, Library

from rifferr.backends import localfs
from rifferr.backends.base import TemplateError
from rifferr.backends.beets import templates
from rifferr.backends.beets.snapshots import item_id, parse_id
from rifferr.core.changeset import FileEffect, FileMode, TargetRef
from rifferr.core.moves import (
    ArtifactRule,
    MovePlan,
    PathFormat,
    Tracked,
    album_folder,
    build_plan,
    check_conflicts,
    shared_folders,
)
from rifferr.core.snapshot import AlbumSnapshot, label


def _real(path: bytes | str) -> str:
    return os.path.realpath(os.fsdecode(path))


def _destination(item: Item, basedir: bytes, formats: list[tuple[str, str]] | None) -> str:
    with templates.capture() as failures:
        try:
            dst = item.destination(basedir=basedir, path_formats=formats)
        except Exception as exc:  # broken template or query
            raise TemplateError(f"path format for {os.fsdecode(item.path)}: {exc}") from exc
    if failures:
        raise TemplateError(f"path format for {os.fsdecode(item.path)}: {failures[0]}")
    return _real(dst)


def plan(
    lib: Library,
    albums: Sequence[AlbumSnapshot],
    *,
    mode: FileMode,
    formats: Sequence[PathFormat] | None,
    directory: Path | None,
    artifacts: Sequence[ArtifactRule],
) -> list[MovePlan]:
    basedir = os.fsencode(str(directory)) if directory is not None else lib.directory
    root = _real(basedir)
    pairs = [(f.query, f.format) for f in formats] if formats else None
    plans: list[MovePlan] = []
    for snap in albums:
        album = lib.get_album(parse_id(snap.id)[1])
        if album is None:
            continue
        ref = TargetRef(scope="album", id=snap.id)
        tracked: list[Tracked] = []
        problems: list[str] = []
        for item in album.items():
            try:
                dst = _destination(item, basedir, pairs)
            except TemplateError as exc:
                problems.append(str(exc))
                continue
            tracked.append(
                (TargetRef(scope="item", id=item_id(item)), "item", _real(item.path), dst)
            )
        if problems or not tracked:
            plans.append(
                MovePlan(
                    album=ref,
                    label=label(snap),
                    src_dir="",
                    dst_dir="",
                    conflicts=problems or ["the album has no items"],
                )
            )
            continue
        if album.artpath:
            item_dir = os.fsencode(album_folder(d for _, _, _, d in tracked))
            art_dst = _real(album.art_destination(album.artpath, item_dir))
            tracked.append((ref, "art", _real(album.artpath), art_dst))
        plans.append(
            build_plan(
                ref,
                label(snap),
                tracked,
                mode=mode,
                artifacts=artifacts,
                list_files=localfs.list_files,
                stat=localfs.stat_hash,
            )
        )
    plans = shared_folders(plans)
    return check_conflicts(plans, root, exists=os.path.lexists, is_dir=os.path.isdir)


def set_paths(lib: Library, effects: Sequence[FileEffect]) -> None:
    """Point items and album art at their new files (library only, no file is touched)."""
    with lib.transaction():
        for e in effects:
            if not e.tracked:
                continue
            kind, number = parse_id(e.target.id)
            obj: Item | Album | None = (
                lib.get_item(number) if e.kind == "item" else lib.get_album(number)
            )
            if obj is None:
                raise KeyError(f"{e.target.id} is no longer in the library")
            if e.kind == "item":
                assert isinstance(obj, Item)
                assert kind == "item"
                obj.path = os.fsencode(e.dst)
            else:
                assert isinstance(obj, Album)
                assert kind == "album"
                obj.artpath = os.fsencode(e.dst)
            obj.store()
