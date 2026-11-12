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
from typing import Any, Final

from beets.library import Album, Item, Library

from rifferr.backends.beets.snapshots import album_id, field_value, item_id, parse_id
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
from rifferr.core.snapshot import FieldValue

#: Bookkeeping fields a ChangeSet can't touch; computed fields are refused too.
READ_ONLY: Final = frozenset({"id", "path", "album_id", "mtime", "added", "artpath"})


def load(lib: Library, ref: TargetRef) -> Item | Album | None:
    try:
        kind, number = parse_id(ref.id)
    except KeyError:
        return None
    if kind != ref.scope:
        return None
    return lib.get_album(number) if kind == "album" else lib.get_item(number)


def check(lib: Library, changes: ChangeSet) -> list[ChangeProblem]:
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
            if c.field in READ_ONLY or c.field in computed:
                message = "read-only field"
            elif c.op == "set":
                message = _type_problem(model, c.field, c.new)
            if message is not None:
                problems.append(ChangeProblem(target=t.target, field=c.field, message=message))
    return problems


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
    return {k: field_value(obj.get(k)) for k in obj.keys(computed=False) if k not in ("id", "path")}


def apply(lib: Library, changes: ChangeSet, *, write_tags: bool) -> ApplyResult:
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
            for c in t.changes:
                if c.op == "delete":
                    if c.field in obj:
                        del obj[c.field]
                else:
                    obj[c.field] = c.new
            obj.store()  # Album.store() carries album fields to its items
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
