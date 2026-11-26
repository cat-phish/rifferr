"""Checking and applying ChangeSets to a beets library (CHANGESET-2, -8, -10, -11).

Album-scoped changes are stored on the ``Album``, so beets carries them to the
album's items (``Album.store(inherit=True)``); item-scoped changes go on the
``Item``. Everything happens in one library transaction. What changed is found
by diffing the album and its items before and after, so fields beets changed on
its own (inheritance) are reported too. File tags are then written with beets'
writer; a failed write keeps the database change and is reported per item.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

from beets.library import Album, Item, Library

from rifferr.artstore import ArtStore, art_problem
from rifferr.backends.beets.snapshots import (
    ART_ADOPTED,
    album_id,
    art_file,
    item_id,
    parse_id,
    stored_fields,
)
from rifferr.core.changeset import (
    ApplyResult,
    ChangeProblem,
    ChangeSet,
    FieldEffect,
    TargetRef,
    WriteFailure,
    diff_effects,
    find_conflicts,
)
from rifferr.core.covers import ART_FIELD, ART_KEPT_FIELD, sha256_hex
from rifferr.core.snapshot import FieldValue

#: Bookkeeping fields a ChangeSet can't touch; computed fields are refused too.
READ_ONLY: Final = frozenset(
    {"id", "path", "album_id", "mtime", "added", "artpath", ART_ADOPTED, ART_KEPT_FIELD}
)


def load(lib: Library, ref: TargetRef) -> Item | Album | None:
    try:
        kind, number = parse_id(ref.id)
    except KeyError:
        return None
    if kind != ref.scope:
        return None
    return lib.get_album(number) if kind == "album" else lib.get_item(number)


def check(lib: Library, changes: ChangeSet, art: ArtStore | None = None) -> list[ChangeProblem]:
    problems: list[ChangeProblem] = []
    for t in changes.targets:
        obj = load(lib, t.target)
        if obj is None:
            problems.append(ChangeProblem(target=t.target, field="", message="not found"))
            continue
        model = type(obj)
        computed = set(model._getters())  # pyright: ignore[reportPrivateUsage]
        for c in t.changes:
            message: str | None = None
            if c.field == ART_FIELD:
                message = art_problem(t.target.scope, c.op, c.new, art)
                if message is None and c.op == "set" and isinstance(obj, Album):
                    message = _folder_problem(obj)
            elif c.field in READ_ONLY or c.field in computed:
                message = "read-only field"
            elif c.op == "set":
                message = _type_problem(model, c.field, c.new)
            if message is not None:
                problems.append(ChangeProblem(target=t.target, field=c.field, message=message))
    return problems


def _folder_problem(album: Album) -> str | None:
    try:
        folder = album.item_dir()
    except ValueError:
        return "the album has no items, so no folder for its cover"
    return None if os.path.isdir(folder) else "the album's folder doesn't exist"


def _type_problem(model: type[Item] | type[Album], field: str, value: FieldValue) -> str | None:
    wanted: Any = model._type(field).model_type  # pyright: ignore[reportPrivateUsage]
    if wanted is bool:
        ok = isinstance(value, bool)
    elif wanted is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif wanted is float:
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    elif wanted is list:
        ok = isinstance(value, list)
    elif wanted is str:
        ok = isinstance(value, str)
    else:
        return f"field type {getattr(wanted, '__name__', wanted)} can't be set"
    if ok:
        return None
    return f"expects {getattr(wanted, '__name__', wanted)}, got {type(value).__name__}"


def fields_of(obj: Item | Album) -> dict[str, FieldValue]:
    """Stored fields as snapshots show them (no computed fields)."""
    return stored_fields(obj)


def apply(
    lib: Library, changes: ChangeSet, *, write_tags: bool, art: ArtStore | None = None
) -> ApplyResult:
    """All or nothing in the library DB; call ``check`` first."""
    with lib.transaction():
        objects = {t.target: load(lib, t.target) for t in changes.targets}

        def current(ref: TargetRef) -> dict[str, FieldValue] | None:
            obj = objects.get(ref)
            return None if obj is None else fields_of(obj)

        conflicts = find_conflicts(changes, current)
        if conflicts:
            return ApplyResult(applied=False, conflicts=conflicts)
        albums = _albums(lib, objects.values())
        before = _state(albums)
        for t in changes.targets:
            obj = objects[t.target]
            assert obj is not None, "check() refuses unknown targets"
            arts = [c for c in t.changes if c.field == ART_FIELD]
            for c in t.changes:
                if c.field == ART_FIELD:
                    continue
                if c.op == "delete":
                    if c.field in obj:
                        del obj[c.field]
                else:
                    obj[c.field] = c.new
            obj.store()  # Album.store() carries album fields to its items
            for c in arts:
                assert art is not None, "check() refuses art without a store"
                assert isinstance(obj, Album), "check() refuses art on items"
                _set_art(obj, art, None if c.op == "delete" else str(c.new))
                obj.store(inherit=False)  # artpath and the adopted mark stay on the album
        reloaded = (lib.get_album(a.id) for a in albums if a.id is not None)
        albums = [a for a in reloaded if a is not None]
        after = _state(albums)
    effects: list[FieldEffect] = []
    for ref, fields in after.items():
        effects += diff_effects(ref, before.get(ref, {}), fields, ignore=READ_ONLY)
    failures: list[WriteFailure] = []
    if write_tags:
        changed = {e.target.id for e in effects if e.target.scope == "item"}
        for album in albums:
            for item in album.items():
                if item_id(item) in changed:
                    failure = _write(item)
                    if failure is not None:
                        failures.append(failure)
    return ApplyResult(applied=True, effects=effects, write_failures=failures)


def _set_art(album: Album, art: ArtStore, sha: str | None) -> None:
    """Set or remove the album's cover (COVERS-SEARCH-16, -17). Any image this would
    replace or delete is copied into the art store first, so undo can bring it back.

    An image already in the album folder with the new bytes is used where it is, not
    copied, and marked as the album's own; removing or replacing such art keeps its file."""
    current = art_file(album)
    adopted = bool(album.get(ART_ADOPTED))
    if current is not None:
        art.put_file(Path(current))
    if ART_ADOPTED in album:
        del album[ART_ADOPTED]
    if sha is None:
        if current is not None and not adopted:
            os.remove(current)
        album.artpath = None
        return
    own = _own_image(album, art, sha)
    if own is not None:
        if current is not None and not adopted and not os.path.samefile(current, own):
            os.remove(current)
        album.artpath = os.fsencode(own)
        album[ART_ADOPTED] = 1
        return
    if adopted and current is not None:
        album.artpath = None  # so beets' set_art doesn't remove the album's own file
        dest = os.fsdecode(album.art_destination(os.fsencode(current)))
        if os.path.normpath(dest) == os.path.normpath(current):
            os.rename(current, _free_name(current, "-original"))  # never overwritten
        current = None
    source = art.find(sha)
    assert source is not None, "check() refuses images not in the store"
    dest = album.art_destination(os.fsencode(source))
    if os.path.isfile(dest) and (current is None or not os.path.samefile(dest, current)):
        art.put_file(Path(os.fsdecode(dest)))  # an image beets would overwrite
    album.set_art(os.fsencode(source), copy=True)


def _free_name(path: str, suffix: str) -> str:
    """``<stem><suffix><ext>`` next to ``path``, numbered when that name is taken."""
    stem, ext = os.path.splitext(path)
    candidate, n = f"{stem}{suffix}{ext}", 2
    while os.path.lexists(candidate):
        candidate, n = f"{stem}{suffix}-{n}{ext}", n + 1
    return candidate


def _own_image(album: Album, art: ArtStore, sha: str) -> str | None:
    """A file directly in the album folder whose bytes are the image ``sha``."""
    source = art.find(sha)
    if source is None:
        return None
    try:
        folder = os.fsdecode(album.item_dir())
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except (ValueError, OSError):
        return None
    size = source.stat().st_size
    for entry in entries:
        if not entry.is_file(follow_symlinks=False) or entry.stat().st_size != size:
            continue
        try:
            if sha256_hex(Path(entry.path).read_bytes()) == sha:
                return entry.path
        except OSError:
            continue
    return None


def _albums(lib: Library, objects: Iterable[Item | Album | None]) -> list[Album]:
    seen: dict[int, Album] = {}
    for obj in objects:
        album = obj if isinstance(obj, Album) else obj.get_album() if obj is not None else None
        if album is not None and album.id is not None:
            seen.setdefault(album.id, album)
    return list(seen.values())


def _state(albums: list[Album]) -> dict[TargetRef, dict[str, FieldValue]]:
    out: dict[TargetRef, dict[str, FieldValue]] = {}
    for album in albums:
        out[TargetRef(scope="album", id=album_id(album))] = fields_of(album)
        for item in album.items():
            out[TargetRef(scope="item", id=item_id(item))] = fields_of(item)
    return out


def _write(item: Item) -> WriteFailure | None:
    try:
        item.write()
        item.store()  # the new mtime, as beets' own try_sync does
    except Exception as exc:  # beets raises ReadError/WriteError (FileOperationError)
        return WriteFailure(
            target=TargetRef(scope="item", id=item_id(item)),
            path=os.fsdecode(item.path),
            error=str(exc),
        )
    return None
