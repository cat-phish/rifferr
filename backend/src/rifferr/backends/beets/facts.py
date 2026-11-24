"""Album facts in beets (C-ALBUM-FACTS): config, artist-name rules and field types.

Config, in the profile's beets config:

    rifferr:
      artist_names: artist-names.yaml   # relative to the beets directory, or absolute
      album_facts:
        live_tracks: "title::(?i)[(]live[)]"   # rf_live_tracks, rf_share_live_tracks

Facts are integers, so the ``rifferr`` beets plugin types them (``album_types``) and beets
queries compare them as numbers: ``rf_items:..6``, ``rf_share_multi_artist_tracks:51..``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import beets
from beets.dbcore import types as dbtypes
from beets.library import Album
from ruamel.yaml import YAML, YAMLError

from rifferr.core.facts import PREFIX, ArtistNames

BUILT_IN = (
    "items",
    "length",
    "joined_albumartist",
    "multi_artist_tracks",
    "share_multi_artist_tracks",
)
_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class FactsConfigError(Exception):
    """``rifferr.album_facts`` or ``rifferr.artist_names`` can't be used."""


def queries() -> dict[str, str]:
    """``rifferr.album_facts``: fact name → beets item query (ALBUM-FACTS-2)."""
    section = beets.config["rifferr"]["album_facts"]
    raw: Any = section.get() if section.exists() else None
    if not raw:
        return {}
    if not isinstance(raw, Mapping):
        raise FactsConfigError("rifferr.album_facts must map a fact name to a beets query")
    out: dict[str, str] = {}
    for name, query in raw.items():  # pyright: ignore[reportUnknownVariableType]
        name = str(name)  # pyright: ignore[reportUnknownArgumentType]
        if not _NAME.match(name) or name in BUILT_IN or name.startswith(("share_", "count_")):
            raise FactsConfigError(
                f"rifferr.album_facts.{name}: use lowercase letters, digits and _, not a "
                "built-in fact name"
            )
        out[name] = str(query)  # pyright: ignore[reportUnknownArgumentType]
    return out


def names_path(beetsdir: Path) -> Path | None:
    """The artist-name rules file, or None when the config names none (ALBUM-FACTS-7)."""
    section = beets.config["rifferr"]["artist_names"]
    if not section.exists() or not section.get():
        return None
    path = Path(str(section.get())).expanduser()
    return path if path.is_absolute() else beetsdir / path


_cache: dict[Path, tuple[float, ArtistNames]] = {}


def load_names(path: Path | None) -> ArtistNames:
    """The rules in ``path``, re-read when the file changes (a review may add a name)."""
    if path is None:
        return ArtistNames()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return ArtistNames()
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
        names = ArtistNames.from_mapping(data)
    except (YAMLError, ValueError, AttributeError) as exc:
        raise FactsConfigError(f"artist names file {path}: {exc}") from None
    _cache[path] = (mtime, names)
    return names


def field_types() -> dict[str, Any]:
    """Integer types for every fact the plugin can know of in advance."""
    names = [*BUILT_IN]
    for name in queries():
        names += [name, f"share_{name}"]
    album_fields: Any = Album._fields  # pyright: ignore[reportPrivateUsage]
    for field, kind in album_fields.items():
        if isinstance(kind, dbtypes.DelimitedString):
            names.append(f"count_{field}")
    return {PREFIX + n: dbtypes.INTEGER for n in names}


def count_field_types(list_fields: list[str]) -> dict[str, Any]:
    return {f"{PREFIX}count_{f}": dbtypes.INTEGER for f in list_fields}
