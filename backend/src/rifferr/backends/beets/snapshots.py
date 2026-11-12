"""beets Item/Album -> snapshot conversion (BACKEND-3, -4)."""

from __future__ import annotations

import math
import os
from typing import Any

from beets.library import Album, Item

from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

ALBUM_PREFIX = "album-"
ITEM_PREFIX = "item-"
_SKIP = {"id", "path"}


def album_id(album: Album) -> str:
    return f"{ALBUM_PREFIX}{album.id}"


def item_id(item: Item) -> str:
    return f"{ITEM_PREFIX}{item.id}"


def parse_id(snapshot_id: str) -> tuple[str, int]:
    for kind, prefix in (("album", ALBUM_PREFIX), ("item", ITEM_PREFIX)):
        if snapshot_id.startswith(prefix) and snapshot_id[len(prefix) :].isdigit():
            return kind, int(snapshot_id[len(prefix) :])
    raise KeyError(f"not a beets snapshot id: {snapshot_id!r}")


def field_value(value: Any) -> FieldValue:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, bytes):
        return os.fsdecode(value)
    if isinstance(value, list | tuple):
        return [str(field_value(v)) for v in value]  # pyright: ignore[reportUnknownVariableType]
    return str(value)


def _fields(obj: Item | Album) -> dict[str, FieldValue]:
    return {k: field_value(obj.get(k)) for k in obj.keys(computed=False) if k not in _SKIP}


def item_snapshot(item: Item) -> ItemSnapshot:
    return ItemSnapshot(id=item_id(item), path=os.fsdecode(item.path), fields=_fields(item))


def _track_key(item: Item) -> tuple[int, int]:
    return (item.get("disc") or 0, item.get("track") or 0)


def album_snapshot(album: Album, items: list[Item] | None = None) -> AlbumSnapshot:
    chosen = sorted(list(album.items()) if items is None else items, key=_track_key)
    try:
        path: str | None = os.fsdecode(album.item_dir())
    except ValueError:  # an album without items
        path = None
    return AlbumSnapshot(
        id=album_id(album),
        path=path,
        fields=_fields(album),
        items=[item_snapshot(i) for i in chosen],
    )
