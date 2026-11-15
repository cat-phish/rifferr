"""Built-in cover providers (S-COVERS-SEARCH COVERS-SEARCH-4).

Official APIs only: Cover Art Archive, the iTunes Search API, Deezer's public API, plus
images already in the album folder. None of them needs a key.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final, cast

from pydantic import JsonValue

from rifferr.core.covers import CoverCandidate, CoverQuery, sniff_image
from rifferr.covers.base import confidence
from rifferr.covers.http import CoverHttp

_HEADER: Final = 1 << 20
"""Bytes read from a local image to find its size (JPEG headers can follow big EXIF)."""


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _year(value: Any) -> int | None:
    m = re.match(r"^(\d{4})", _str(value))
    return int(m.group(1)) if m else None


class CoverArtArchive:
    """Front images by MusicBrainz release id, then release group id."""

    name = "coverartarchive"
    hosts = frozenset({"coverartarchive.org", "archive.org", ".archive.org"})
    needs = False

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        for kind, mbid in (
            ("release", query.mb_albumid),
            ("release-group", query.mb_releasegroupid),
        ):
            if not mbid:
                continue
            data = await http.get_json(f"https://coverartarchive.org/{kind}/{mbid}")
            if not isinstance(data, dict):
                continue
            body = cast(dict[str, Any], data)
            source = _str(body.get("release")) or f"https://musicbrainz.org/{kind}/{mbid}"
            found = False
            for image in cast(list[Any], body.get("images") or []):
                if not isinstance(image, dict):
                    continue
                img = cast(dict[str, Any], image)
                types = cast(list[Any], img.get("types") or [])
                if not (img.get("front") is True or "Front" in types):
                    continue
                url = _str(img.get("image"))
                if not url:
                    continue
                thumbs = cast(dict[str, Any], img.get("thumbnails") or {})
                thumb = _str(thumbs.get("500")) or _str(thumbs.get("large")) or None
                found = True
                yield CoverCandidate.make(
                    self.name,
                    image_url=url.replace("http://", "https://", 1),
                    thumb_url=thumb.replace("http://", "https://", 1) if thumb else None,
                    match_confidence=1.0,
                    source_url=source,
                    title=f"MusicBrainz {kind} {mbid}",
                )
            if found:
                return  # the release's own art wins over its group's


class ITunes:
    """The iTunes Search API, entity ``album``. Settings: ``country`` (default US),
    ``size`` (pixels asked for, default 1200), ``limit`` (default 10)."""

    name = "itunes"
    hosts = frozenset({"itunes.apple.com", ".mzstatic.com"})
    needs = False

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        term = " ".join(p for p in (query.artist, query.album) if p)
        if not term:
            return
        params = {
            "term": term,
            "entity": "album",
            "media": "music",
            "country": str(settings.get("country") or "US"),
            "limit": str(settings.get("limit") or 10),
        }
        data = await http.get_json("https://itunes.apple.com/search", params)
        size = int(cast(int, settings.get("size") or 1200))
        for result in cast(list[Any], (data or {}).get("results") or []):
            if not isinstance(result, dict):
                continue
            r = cast(dict[str, Any], result)
            art = _str(r.get("artworkUrl100"))
            if not art:
                continue
            artist, album = _str(r.get("artistName")), _str(r.get("collectionName"))
            year = _year(r.get("releaseDate"))
            yield CoverCandidate.make(
                self.name,
                image_url=re.sub(r"/\d+x\d+bb\.", f"/{size}x{size}bb.", art),
                thumb_url=art.replace("100x100bb", "300x300bb"),
                match_confidence=confidence(query, artist, album, year, settings),
                source_url=_str(r.get("collectionViewUrl")) or None,
                title=f"{artist} - {album}" + (f" ({year})" if year else ""),
            )


class Deezer:
    """Deezer's public search API, type ``album``; ``cover_xl`` is 1000x1000."""

    name = "deezer"
    hosts = frozenset({"api.deezer.com", ".dzcdn.net"})
    needs = False

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        if not query.album:
            return
        q = f'album:"{query.album}"'
        if query.artist:
            q = f'artist:"{query.artist}" {q}'
        data = await http.get_json(
            "https://api.deezer.com/search/album",
            {"q": q, "limit": str(settings.get("limit") or 10)},
        )
        for result in cast(list[Any], (data or {}).get("data") or []):
            if not isinstance(result, dict):
                continue
            r = cast(dict[str, Any], result)
            url = _str(r.get("cover_xl"))
            if not url:
                continue
            artist = _str(cast(dict[str, Any], r.get("artist") or {}).get("name"))
            album = _str(r.get("title"))
            yield CoverCandidate.make(
                self.name,
                image_url=url,
                thumb_url=_str(r.get("cover_medium")) or None,
                width=1000,
                height=1000,
                match_confidence=confidence(
                    query, artist, album, _year(r.get("release_date")), settings
                ),
                source_url=_str(r.get("link")) or None,
                title=f"{artist} - {album}",
            )


DEFAULT_PATTERNS: Final = ("*.jpg", "*.jpeg", "*.png")


class LocalFiles:
    """Images in the album folder (not below it). Setting ``patterns``: file-name globs,
    no case (default ``*.jpg``, ``*.jpeg``, ``*.png``); earlier patterns rank first."""

    name = "local"
    hosts: frozenset[str] = frozenset()
    needs = False

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        for path, info in local_images(query.folder, settings.get("patterns")):
            yield CoverCandidate.make(
                self.name,
                path=str(path),
                width=info[0],
                height=info[1],
                match_confidence=1.0,
                title=path.name,
            )


def local_images(
    folder: str | None, patterns: JsonValue = None
) -> list[tuple[Path, tuple[int, int]]]:
    """Image files directly in ``folder`` matching ``patterns``, in pattern order, with
    their sizes. Symbolic links and unreadable or non-image files are left out."""
    if not folder:
        return []
    root = Path(folder)
    if not root.is_dir():
        return []
    globs = [str(p) for p in cast(list[Any], patterns)] if isinstance(patterns, list) else None
    globs = globs or list(DEFAULT_PATTERNS)
    files = sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink())
    out: list[tuple[Path, tuple[int, int]]] = []
    seen: set[Path] = set()
    for pattern in globs:
        for p in files:
            if p in seen or not fnmatch.fnmatch(p.name.lower(), pattern.lower()):
                continue
            try:
                with p.open("rb") as f:
                    info = sniff_image(f.read(_HEADER))
            except OSError:
                continue
            if info is None:
                continue
            seen.add(p)
            out.append((p, (info.width, info.height)))
    return out


BUILTINS: Final = (CoverArtArchive, ITunes, Deezer, LocalFiles)
