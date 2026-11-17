"""Cover search models and ranking (S-COVERS-SEARCH). Pure: no network, no files.

Providers yield ``CoverCandidate``s for a ``CoverQuery``; ``rank`` drops candidates that
fail the hard filters and orders the rest by ``weight * match_confidence * size_score``
(COVERS-SEARCH-7 to -10). A chosen cover reaches the library as a ChangeSet that sets the
album's ``ART_FIELD`` to the image's sha256 (COVERS-SEARCH-15).
"""

from __future__ import annotations

import hashlib
import re
import struct
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, JsonValue, field_validator

from rifferr.core.duration import Duration

ART_FIELD: Final = "art_sha256"
"""The album field that names its cover by the image's sha256 (COVERS-SEARCH-15). Backends
with the ``art`` capability compute it from the album's art file and set the art when a
ChangeSet changes it; ``delete`` removes the art."""
SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
BUILTIN_PROVIDERS: Final = ("coverartarchive", "local", "itunes", "deezer")
Mime = Literal["image/jpeg", "image/png"]
EXTENSIONS: Final[dict[str, str]] = {"image/jpeg": ".jpg", "image/png": ".png"}

_SIZE: Final = re.compile(r"^(\d+)\s*(B|KB|MB|GB)?$", re.IGNORECASE)
_UNITS: Final = {"": 1, "b": 1, "kb": 1000, "mb": 1000**2, "gb": 1000**3}


def parse_size(value: int | str) -> int:
    """``"25MB"`` -> 25_000_000 bytes; an int is bytes. Raises ``ValueError``."""
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("a size must be more than zero")
        return value
    m = _SIZE.fullmatch(value.strip())
    if not m:
        raise ValueError(f"{value!r} is not a size like 500KB, 25MB or 1GB")
    size = int(m.group(1)) * _UNITS[(m.group(2) or "").lower()]
    if size <= 0:
        raise ValueError("a size must be more than zero")
    return size


def _check_size(value: int | str) -> int | str:
    parse_size(value)
    return value


ByteSize = Annotated[
    int | str,
    AfterValidator(_check_size),
    Field(description="Bytes, or a number with KB, MB or GB, e.g. 25MB"),
]


class Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pattern: str = Field(description="A regular expression")
    replace: str = ""


class ProviderSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    weight: float = Field(default=1, ge=0, le=10)
    settings: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


def _default_providers() -> list[ProviderSetting]:
    return [ProviderSetting(name=n) for n in BUILTIN_PROVIDERS]


class CoverSettings(BaseModel):
    """``covers`` in ``profile.yaml`` (COVERS-SEARCH-5, -7 to -11)."""

    model_config = ConfigDict(extra="forbid")

    contact: str | None = Field(default=None, description="Added to the User-Agent")
    timeout: Duration = "15s"
    max_bytes: ByteSize = "25MB"
    target_size: int = Field(default=1000, gt=0, description="Short side that scores 1.0")
    min_size: int = Field(default=500, ge=0, description="Smallest short side kept (px)")
    max_aspect_error: float = Field(default=0.1, ge=0)
    unknown_size_score: float = Field(default=0.5, ge=0, le=1)
    year_penalty: float = Field(default=0.8, ge=0, le=1)
    cache_ttl: Duration = "7d"
    cache_max: ByteSize = "500MB"
    normalize: list[Replacement] = Field(
        default_factory=list[Replacement],
        description="Extra replacements applied to names before comparing them",
    )
    providers: list[ProviderSetting] = Field(
        default_factory=_default_providers, description="In order; the order breaks ties"
    )

    def provider(self, name: str) -> ProviderSetting | None:
        return next((p for p in self.providers if p.name == name), None)


class CoverQuery(BaseModel):
    """What to look for (COVERS-SEARCH-2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artist: str = ""
    album: str = ""
    year: int | None = None
    catalognum: str | None = None
    barcode: str | None = None
    mb_albumid: str | None = None
    mb_releasegroupid: str | None = None
    folder: str | None = None

    def key(self) -> str:
        """A cache key: the normalised query (COVERS-SEARCH-11)."""
        parts = [
            normalize_name(self.artist),
            normalize_name(self.album),
            str(self.year or ""),
            (self.catalognum or "").strip().lower(),
            (self.barcode or "").strip(),
            (self.mb_albumid or "").strip().lower(),
            (self.mb_releasegroupid or "").strip().lower(),
            self.folder or "",
        ]
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def web_url_or_none(value: str | None) -> str | None:
    """SEC-44: a link from a provider is kept only when its scheme is http or https."""
    if value is None:
        return None
    return value if urlsplit(value.strip()).scheme.lower() in ("http", "https") else None


class CoverCandidate(BaseModel):
    """One image a provider offers (COVERS-SEARCH-3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    provider: str
    title: str | None = None
    image_url: str | None = None
    path: str | None = None
    thumb_url: str | None = None
    width: int | None = None
    height: int | None = None
    match_confidence: float = Field(ge=0, le=1)
    source_url: str | None = None

    @field_validator("source_url")
    @classmethod
    def _web_link_only(cls, v: str | None) -> str | None:
        return web_url_or_none(v)

    @classmethod
    def make(
        cls, provider: str, *, image_url: str | None = None, path: str | None = None, **rest: object
    ) -> CoverCandidate:
        ref = image_url if image_url is not None else path
        if ref is None:
            raise ValueError("a candidate needs image_url or path")
        return cls.model_validate(
            {
                "id": candidate_id(provider, ref),
                "provider": provider,
                "image_url": image_url,
                "path": path,
                **rest,
            }
        )


FilterReason = Literal["small", "aspect"]


class RankedCandidate(CoverCandidate):
    score: float
    size_score: float | None = Field(
        default=None, description="The size part of the score (COVER-PICKER-8)"
    )
    filtered: FilterReason | None = Field(
        default=None, description="Why the hard filters hide it, or null"
    )


def candidate_id(provider: str, ref: str) -> str:
    return hashlib.sha256(f"{provider}\x1f{ref}".encode()).hexdigest()[:24]


# -- matching (COVERS-SEARCH-8) ------------------------------------------------------

_PUNCT: Final = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE: Final = re.compile(r"\s+")
_THE: Final = re.compile(r"^the\s+")


def normalize_name(text: str, extra: Iterable[Replacement] = ()) -> str:
    """Lower case, no diacritics or punctuation, no leading "the": for comparing only."""
    for r in extra:
        text = re.sub(r.pattern, r.replace, text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = _SPACE.sub(" ", _PUNCT.sub(" ", text)).strip()
    return _THE.sub("", text)


def _similar(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def match_score(
    query: CoverQuery,
    artist: str,
    album: str,
    year: int | None = None,
    *,
    extra: Iterable[Replacement] = (),
    year_penalty: float = 0.8,
) -> float:
    """0 to 1: how well a provider's artist/album strings match the query."""
    rules = list(extra)
    scores = [_similar(normalize_name(query.album, rules), normalize_name(album, rules))]
    if query.artist:
        scores.append(_similar(normalize_name(query.artist, rules), normalize_name(artist, rules)))
    product = 1.0
    for s in scores:
        product *= s
    score = product ** (1 / len(scores))
    if query.year is not None and year is not None and abs(query.year - year) > 1:
        score *= year_penalty
    return round(score, 4)


# -- ranking (COVERS-SEARCH-7, -9, -10) ----------------------------------------------


def filter_reason(width: int | None, height: int | None, s: CoverSettings) -> FilterReason | None:
    """Why the hard filters drop an image of this size, or ``None`` (unknown sizes pass)."""
    if width is None or height is None or width <= 0 or height <= 0:
        return None
    if min(width, height) < s.min_size:
        return "small"
    if abs(width / height - 1) > s.max_aspect_error:
        return "aspect"
    return None


def size_score(width: int | None, height: int | None, s: CoverSettings) -> float:
    if width is None or height is None or width <= 0 or height <= 0:
        return s.unknown_size_score
    return min(1.0, min(width, height) / s.target_size)


@dataclass
class Ranking:
    ranked: list[RankedCandidate] = field(default_factory=list[RankedCandidate])
    hidden: list[RankedCandidate] = field(default_factory=list[RankedCandidate])
    """Candidates the hard filters dropped, scored as if they hadn't (COVER-PICKER-8)."""
    dropped: dict[str, dict[str, int]] = field(default_factory=dict[str, dict[str, int]])
    """provider -> filter reason -> count."""


def rank(
    candidates: Iterable[CoverCandidate],
    settings: CoverSettings,
    weights: Mapping[str, float] | None = None,
    order: list[str] | None = None,
) -> Ranking:
    """Filter, score and sort. ``weights``/``order`` default to the profile's providers."""
    enabled = [p for p in settings.providers if p.enabled]
    weights = dict(weights) if weights is not None else {p.name: p.weight for p in enabled}
    order = order if order is not None else [p.name for p in enabled]
    position = {name: i for i, name in enumerate(order)}
    out = Ranking()
    seen: set[str] = set()
    for c in candidates:
        if c.id in seen:
            continue
        seen.add(c.id)
        ranked = score_candidate(c, settings, weights.get(c.provider, 1.0))
        if ranked.filtered is not None:
            counts = out.dropped.setdefault(c.provider, {})
            counts[ranked.filtered] = counts.get(ranked.filtered, 0) + 1
            out.hidden.append(ranked)
            continue
        out.ranked.append(ranked)

    def key(c: RankedCandidate) -> tuple[float, int, int]:
        area = (c.width or 0) * (c.height or 0)
        return (-c.score, position.get(c.provider, len(position)), -area)

    out.ranked.sort(key=key)
    out.hidden.sort(key=key)
    return out


def score_candidate(c: CoverCandidate, settings: CoverSettings, weight: float) -> RankedCandidate:
    """``c`` with its score, size score and hard-filter reason (COVERS-SEARCH-7, COVER-PICKER-8)."""
    sized = size_score(c.width, c.height, settings)
    return RankedCandidate(
        **c.model_dump(),
        score=round(weight * c.match_confidence * sized, 4),
        size_score=round(sized, 4),
        filtered=filter_reason(c.width, c.height, settings),
    )


# -- images ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImageInfo:
    mime: Mime
    width: int
    height: int


_SOF: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def sniff_image(data: bytes) -> ImageInfo | None:
    """A JPEG or PNG's type and size from its header (magic bytes), else ``None``."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("image/png", width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data)
    return None


def _jpeg(data: bytes) -> ImageInfo | None:
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _SOF:
            if i + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return ImageInfo("image/jpeg", width, height) if width and height else None
        if length < 2:
            return None
        i += 2 + length
    return None


def mime_of(data: bytes) -> Mime | None:
    """The image type by magic bytes alone."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    return None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- review items (COVERS-SEARCH-14, -21) --------------------------------------------

CoverReviewState = Literal["pending", "chosen", "skipped", "failed", "cancelled"]
CoverChoice = Literal["choose", "skip", "fail"]


class CoverReview(BaseModel):
    """An album waiting for a person to pick its cover (COVERS-SEARCH-14)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    profile: str
    run_id: str
    step_id: str
    album_id: str
    label: str
    state: CoverReviewState = "pending"
    query: CoverQuery
    candidates: list[RankedCandidate]
    dropped: dict[str, dict[str, int]] = Field(default_factory=dict[str, dict[str, int]])
    old_sha256: str | None = None
    reason: str = Field(default="", description="Why it waits, e.g. no candidate scored enough")
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    chosen: str | None = Field(default=None, description="The chosen image's sha256")

    def hold_key(self) -> str:
        return self.album_id
