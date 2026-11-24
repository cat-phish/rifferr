"""Computed album facts (C-ALBUM-FACTS): pure functions over an album's working values.

Backends compute named counts (beets item queries) and pass them in; everything else
is derived here, so FakeBackend and BeetsBackend agree (ALBUM-FACTS-6).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rifferr.core.snapshot import FieldValue

#: Every fact's name starts with this; tag scripts can't set such fields (ALBUM-FACTS-5).
PREFIX = "rf_"

Fields = Mapping[str, FieldValue]


@dataclass(frozen=True)
class ArtistNames:
    """Artist-name rules (ALBUM-FACTS-7): texts that may join two artist names, and names
    known to be one artist although they contain one. Nothing is built in."""

    joiners: tuple[str, ...] = ()
    names: tuple[str, ...] = ()
    _known: re.Pattern[str] | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        names = sorted({n for n in self.names if n.strip()}, key=len, reverse=True)
        if names:
            pattern = re.compile("|".join(re.escape(n) for n in names), re.IGNORECASE)
            object.__setattr__(self, "_known", pattern)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> ArtistNames:
        """``{joiners: [...], names: [...]}`` as a rules file holds it; bad shapes raise."""
        if not data:
            return cls()
        joiners: object = data.get("joiners") or []
        names: object = data.get("names") or []
        if not isinstance(joiners, list) or not isinstance(names, list):
            raise ValueError("artist names: 'joiners' and 'names' must be lists")
        return cls(
            joiners=tuple(str(j) for j in joiners if str(j)),  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            names=tuple(str(n) for n in names),  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        )

    def _segments(self, text: str) -> list[tuple[str, bool]]:
        """``text`` as (piece, is_known_name) in order."""
        if self._known is None:
            return [(text, False)]
        out: list[tuple[str, bool]] = []
        pos = 0
        for m in self._known.finditer(text):
            out.append((text[pos : m.start()], False))
            out.append((m.group(0), True))
            pos = m.end()
        out.append((text[pos:], False))
        return out

    def _joiner_re(self) -> re.Pattern[str]:
        ordered = sorted(self.joiners, key=len, reverse=True)
        return re.compile("|".join(re.escape(j) for j in ordered), re.IGNORECASE)

    def has_joiner(self, text: str) -> bool:
        """``text`` contains a joiner outside every known name (ALBUM-FACTS-8)."""
        if not self.joiners:
            return False
        joiner = self._joiner_re()
        return any(not known and joiner.search(p) for p, known in self._segments(text))

    def split(self, text: str) -> list[str]:
        """``text`` cut at the joiners outside known names, trimmed, in order."""
        if not self.joiners:
            return [text.strip()] if text.strip() else []
        joiner = self._joiner_re()
        parts: list[str] = []
        current = ""
        for piece, known in self._segments(text):
            if known:
                current += piece
                continue
            first, *rest = joiner.split(piece)
            current += first
            for nxt in rest:
                parts.append(current)
                current = nxt
        parts.append(current)
        return [p.strip() for p in parts if p.strip()]


def _text(value: FieldValue) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(value)
    return str(value)


def multi_artist(item: Fields, names: ArtistNames) -> bool:
    """ALBUM-FACTS-9: two credited artists still in the ``artist`` text (featured artists
    have moved into the title), or, without such credits, a joiner outside known names."""
    artist = _text(item.get("artist"))
    credits = item.get("artists")
    credits = credits if isinstance(credits, list) else []
    if len(credits) >= 2:
        text = artist.casefold()
        return sum(1 for c in credits if c.strip() and c.casefold() in text) >= 2
    return names.has_joiner(artist)


def _share(count: int, total: int) -> int:
    return (100 * count) // total if total else 0


def album_facts(
    album: Fields,
    items: Sequence[Fields],
    names: ArtistNames,
    counts: Mapping[str, int] | None = None,
) -> dict[str, FieldValue]:
    """Every fact of ALBUM-FACTS-1, -2 and -8 to -10, as integers."""
    total = len(items)
    length = 0.0
    for item in items:
        value = item.get("length")
        if isinstance(value, int | float) and not isinstance(value, bool):
            length += float(value)
    multi = sum(1 for item in items if multi_artist(item, names))
    out: dict[str, FieldValue] = {
        f"{PREFIX}items": total,
        f"{PREFIX}length": int(length),
        f"{PREFIX}joined_albumartist": int(names.has_joiner(_text(album.get("albumartist")))),
        f"{PREFIX}multi_artist_tracks": multi,
        f"{PREFIX}share_multi_artist_tracks": _share(multi, total),
    }
    for key, value in album.items():
        if isinstance(value, list) and not key.startswith(PREFIX):
            out[f"{PREFIX}count_{key}"] = len(value)
    for name, count in (counts or {}).items():
        out[f"{PREFIX}{name}"] = count
        out[f"{PREFIX}share_{name}"] = _share(count, total)
    return out
