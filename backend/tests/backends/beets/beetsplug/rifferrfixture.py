"""A beets metadata source for contract tests: candidates from a JSON file, no network.

Enable with ``pluginpath: [<this folder>]`` and ``plugins: [rifferrfixture]``; the
file named by ``RIFFERR_FIXTURE_RELEASES`` holds a list of releases:
``{"album_id", "album", "artist", "year", "search": bool, "tracks": [{"title", "length"}]}``.
Releases with ``search: false`` are found only by id (``search_ids``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Sequence
from typing import Any

from beets.autotag.hooks import AlbumInfo, TrackInfo
from beets.library import Item
from beets.metadata_plugins import MetadataSourcePlugin


def _releases() -> list[dict[str, Any]]:
    path = os.environ.get("RIFFERR_FIXTURE_RELEASES")
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        return list(json.load(f))


def _info(release: dict[str, Any]) -> AlbumInfo:
    tracks = [
        TrackInfo(
            title=t["title"],
            length=float(t.get("length", 1.0)),
            index=n,
            medium=1,
            medium_index=n,
            medium_total=len(release["tracks"]),
            track_id=f"{release['album_id']}-{n}",
            artist=release["artist"],
            data_source="RifferrFixture",
        )
        for n, t in enumerate(release["tracks"], start=1)
    ]
    return AlbumInfo(
        tracks,
        album=release["album"],
        album_id=release["album_id"],
        artist=release["artist"],
        artist_id=f"artist-{release['artist']}",
        year=release.get("year"),
        label=release.get("label"),
        mediums=1,
        data_source="RifferrFixture",
        data_url=f"https://fixture.invalid/release/{release['album_id']}",
    )


class RifferrFixturePlugin(MetadataSourcePlugin):
    def album_for_id(self, album_id: str) -> AlbumInfo | None:
        for release in _releases():
            if release["album_id"] == album_id:
                return _info(release)
        return None

    def track_for_id(self, track_id: str) -> TrackInfo | None:
        return None

    def candidates(
        self, items: Sequence[Item], artist: str, album: str, va_likely: bool
    ) -> Iterable[AlbumInfo]:
        return [_info(r) for r in _releases() if r.get("search", True)]

    def item_candidates(self, item: Item, artist: str, title: str) -> Iterable[TrackInfo]:
        return []
