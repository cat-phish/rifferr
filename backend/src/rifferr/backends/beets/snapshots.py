"""beets Item/Album -> snapshot conversion (BACKEND-3, -4)."""

from __future__ import annotations

import hashlib
import math
import os
from typing import Any, Final

from beets.library import Album, Item

from rifferr.core.covers import ART_FIELD, ART_KEPT_FIELD
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

ALBUM_PREFIX = "album-"
ITEM_PREFIX = "item-"
ART_ADOPTED: Final = "rifferr_art_adopted"
"""Album flexible attribute: its art file is an image the album came with, used in place
(COVERS-SEARCH-16). Removing the art keeps that file. Hidden from snapshots."""
_SKIP = {"id", "path", ART_ADOPTED}


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


_ART_HASHES: dict[tuple[str, int, int], str] = {}


def art_file(album: Album) -> str | None:
    """The album's art file, when ``artpath`` names one that exists."""
    raw = album.get("artpath")
    if not raw:
        return None
    path = os.fsdecode(raw)
    return path if os.path.isfile(path) else None


def art_sha256(album: Album) -> str | None:
    """sha256 of the album's art file (``ART_FIELD``), cached by path, size and time."""
    path = art_file(album)
    if path is None:
        return None
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    found = _ART_HASHES.get(key)
    if found is None:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
        found = h.hexdigest()
        if len(_ART_HASHES) > 10_000:
            _ART_HASHES.clear()
        _ART_HASHES[key] = found
    return found


def stored_fields(obj: Item | Album) -> dict[str, FieldValue]:
    fields = {k: field_value(obj.get(k)) for k in obj.keys(computed=False) if k not in _SKIP}
    if isinstance(obj, Album):
        fields.pop(ART_FIELD, None)
        sha = art_sha256(obj)
        if sha is not None:
            fields[ART_FIELD] = sha
            if obj.get(ART_ADOPTED) and _kept_on_replace(obj):
                fields[ART_KEPT_FIELD] = True
    return fields


def _kept_on_replace(album: Album) -> bool:
    """The album's own art file isn't where beets puts a new cover, so replacing the cover
    leaves it in place (COVERS-SEARCH-16)."""
    current = art_file(album)
    if current is None:
        return False
    dest = os.fsdecode(album.art_destination(os.fsencode(current)))
    return os.path.normpath(dest) != os.path.normpath(current)


def item_snapshot(item: Item) -> ItemSnapshot:
    return ItemSnapshot(id=item_id(item), path=os.fsdecode(item.path), fields=stored_fields(item))


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
        fields=stored_fields(album),
        items=[item_snapshot(i) for i in chosen],
    )
