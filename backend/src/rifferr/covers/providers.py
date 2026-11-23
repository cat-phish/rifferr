"""Built-in cover providers (S-COVERS-SEARCH COVERS-SEARCH-4).

Official APIs only: Cover Art Archive, the iTunes Search API, Deezer's public API, plus
images already in the album folder need no key; Discogs and fanart.tv need one
(COVERS-SEARCH-28, -29).
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


_MBID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


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
            if not mbid or not _MBID.fullmatch(mbid):
                continue
            mbid = mbid.lower()
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


_DISCOGS_API: Final = "https://api.discogs.com"
_DISCOGS_ID: Final = re.compile(r"\d{1,12}")
_DISCOGS_SUFFIX: Final = re.compile(r"\s*\(\d+\)$|\*$")
"""Discogs' "Name (2)" disambiguation and "Name*" name-variation marks."""


def _discogs_name(name: str) -> str:
    return _DISCOGS_SUFFIX.sub("", name.strip()).strip()


def _discogs_image(value: Any) -> str | None:
    """An image URL on Discogs' image host; its "no image" spacer lives elsewhere."""
    url = _str(value)
    return url if url.startswith("https://i.discogs.com/") else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class Discogs:
    """The Discogs API (COVERS-SEARCH-28), with a personal access token. The release
    beets matched, by ``discogs_albumid``; else a release search by artist and title,
    barcode and catalog number. Setting ``max_results`` (default 5)."""

    name = "discogs"
    hosts = frozenset({"api.discogs.com", "i.discogs.com"})
    needs = True
    min_interval = 1.0

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        headers = {"Authorization": f"Discogs token={settings.get('token')}"}
        release_id = (query.discogs_albumid or "").strip()
        if _DISCOGS_ID.fullmatch(release_id):
            data = await http.get_json(f"{_DISCOGS_API}/releases/{release_id}", headers=headers)
            found = self._release(cast(dict[str, Any], data)) if isinstance(data, dict) else None
            if found is not None:
                yield found
                return
        limit = settings.get("max_results")
        limit = limit if isinstance(limit, int) and limit > 0 else 5
        searches: list[dict[str, str]] = []
        if query.album:
            by_name = {"release_title": query.album}
            if query.artist:
                by_name["artist"] = query.artist
            searches.append(by_name)
        if query.barcode:
            searches.append({"barcode": query.barcode})
        if query.catalognum:
            searches.append({"catno": query.catalognum})
        seen: set[int] = set()
        for params in searches:
            if len(seen) >= limit:
                return
            data = await http.get_json(
                f"{_DISCOGS_API}/database/search",
                {"type": "release", "per_page": str(limit), **params},
                headers=headers,
            )
            for result in cast(list[Any], (data or {}).get("results") or []):
                if not isinstance(result, dict):
                    continue
                r = cast(dict[str, Any], result)
                rid = _int(r.get("id"))
                url = _discogs_image(r.get("cover_image"))
                if rid is None or rid in seen or url is None:
                    continue
                seen.add(rid)
                title = _str(r.get("title"))
                artist, sep, album = title.partition(" - ")
                if not sep:
                    artist, album = "", title
                year = _year(str(r.get("year") or ""))
                uri = _str(r.get("uri"))
                yield CoverCandidate.make(
                    self.name,
                    image_url=url,
                    thumb_url=_discogs_image(r.get("thumb")),
                    match_confidence=confidence(
                        query, _discogs_name(artist), album, year, settings
                    ),
                    source_url=f"https://www.discogs.com{uri}" if uri.startswith("/") else None,
                    title=title + (f" ({year})" if year else ""),
                )
                if len(seen) >= limit:
                    return

    def _release(self, data: dict[str, Any]) -> CoverCandidate | None:
        images = [
            cast(dict[str, Any], i)
            for i in cast(list[Any], data.get("images") or [])
            if isinstance(i, dict) and _discogs_image(cast(dict[str, Any], i).get("uri"))
        ]
        if not images:
            return None
        image = next((i for i in images if i.get("type") == "primary"), images[0])
        artists = [
            _discogs_name(_str(cast(dict[str, Any], a).get("name")))
            for a in cast(list[Any], data.get("artists") or [])
            if isinstance(a, dict)
        ]
        title = " / ".join(a for a in artists if a) + " - " + _str(data.get("title"))
        year = _int(data.get("year"))
        return CoverCandidate.make(
            self.name,
            image_url=_discogs_image(image.get("uri")),
            thumb_url=_discogs_image(image.get("uri150")),
            width=_int(image.get("width")),
            height=_int(image.get("height")),
            match_confidence=1.0,
            source_url=_str(data.get("uri")) or None,
            title=title + (f" ({year})" if year else ""),
        )


class FanartTv:
    """fanart.tv album covers by MusicBrainz release group (COVERS-SEARCH-29), with an
    API key. Most liked first."""

    name = "fanarttv"
    hosts = frozenset({"webservice.fanart.tv", "assets.fanart.tv"})
    needs = True
    secret_params = frozenset({"api_key"})

    async def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]:
        mbid = query.mb_releasegroupid
        if not mbid or not _MBID.fullmatch(mbid):
            return
        mbid = mbid.lower()
        data = await http.get_json(
            f"https://webservice.fanart.tv/v3/music/albums/{mbid}",
            {"api_key": str(settings.get("token") or "")},
        )
        albums = (
            cast(dict[str, Any], (data or {}).get("albums") or {}) if isinstance(data, dict) else {}
        )
        group = albums.get(mbid)
        if not isinstance(group, dict):
            return
        covers = [
            cast(dict[str, Any], c)
            for c in cast(list[Any], cast(dict[str, Any], group).get("albumcover") or [])
            if isinstance(c, dict)
        ]

        def likes(c: dict[str, Any]) -> int:
            value = _str(c.get("likes"))
            return int(value) if value.isdigit() else 0

        for c in sorted(covers, key=likes, reverse=True):
            url = _str(c.get("url")).replace("http://", "https://", 1)
            if not url.startswith("https://assets.fanart.tv/"):
                continue
            yield CoverCandidate.make(
                self.name,
                image_url=url,
                match_confidence=1.0,
                source_url=f"https://musicbrainz.org/release-group/{mbid}",
                title=f"fanart.tv, {likes(c)} likes",
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


BUILTINS: Final = (CoverArtArchive, ITunes, Deezer, LocalFiles, Discogs, FanartTv)
