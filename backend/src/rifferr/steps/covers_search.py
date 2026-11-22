"""The ``covers.search`` step (S-COVERS-SEARCH COVERS-SEARCH-12 to -15).

Once per album: skip albums that already have art, search the profile's cover providers,
and either take the best image (``choose: auto``) or leave the album in the review queue
to pick one (``choose: review``). A chosen image goes into the profile's art store and is
proposed as a ChangeSet on the album's ``art_sha256``, so it is journaled, reviewable and
undoable like any tag change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from rifferr.artstore import ArtStore
from rifferr.backends.base import BackendCapabilities, LibraryBackend, TemplateError
from rifferr.core.changeset import ChangeSetProposal, ProposedFieldChange, TargetRef
from rifferr.core.covers import (
    ART_FIELD,
    CoverQuery,
    CoverSettings,
    RankedCandidate,
    parse_size,
    size_score,
)
from rifferr.core.duration import parse_duration
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.core.workflow.template_fields import TEMPLATE_EXTRA
from rifferr.covers.base import CoverProvider
from rifferr.covers.cache import CoverCache
from rifferr.covers.providers import local_images
from rifferr.covers.registry import load_providers
from rifferr.covers.search import (
    DownloadError,
    Image,
    SearchResult,
    download,
    plan,
    search,
)
from rifferr.engine import StepContext, StepResult
from rifferr.engine.steps import CoverRequest, StepFailure

MAX_KEPT: Final = 60
"""Candidates stored with a cover review (COVERS-SEARCH-14)."""
QueryField = Literal[
    "artist", "album", "year", "catalognum", "barcode", "mb_albumid", "mb_releasegroupid"
]
#: Backend templates (beets syntax) that fill each query field by default (COVERS-SEARCH-2).
DEFAULT_QUERY: Final[dict[str, str]] = {
    "artist": "$albumartist",
    "album": "$album",
    "year": "$year",
    "catalognum": "$catalognum",
    "barcode": "$barcode",
    "mb_albumid": "$mb_albumid",
    "mb_releasegroupid": "$mb_releasegroupid",
}


class CoversSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    only_missing: bool = Field(default=True, description="Skip albums that already have art")
    local_counts: bool = Field(
        default=True,
        description="With only_missing: an image in the album folder counts as art",
    )
    choose: Literal["auto", "review"] = Field(
        default="auto", description="auto: take the best cover; review: always ask"
    )
    min_score: float = Field(default=0.6, ge=0, description="Lowest score auto may take")
    max_tries: int = Field(default=3, ge=1, le=20, description="Images auto downloads at most")
    on_none: Literal["review", "skip", "fail"] = Field(
        default="review", description="When auto finds no cover good enough"
    )
    providers: list[str] | None = Field(
        default=None, description="Providers to ask, in order (default: the profile's)"
    )
    query: dict[QueryField, str] = Field(
        default_factory=dict[QueryField, str],
        description="Backend templates for the query fields (beets: $albumartist ...)",
        json_schema_extra=TEMPLATE_EXTRA,
    )
    refresh: bool = Field(default=False, description="Ignore cached search results")


@dataclass(frozen=True)
class Choice:
    candidate: RankedCandidate
    image: Image
    score: float


HttpFactory = Callable[[], httpx.AsyncClient]


class CoversSearchStep:
    name = "covers.search"
    waits = "always"  # may put items in the review queue (UI-REFRESH-13)
    params_model = CoversSearchParams
    requires = BackendCapabilities(query=True, templates=True, apply=True, art=True)
    default_apply = "auto"  # COVERS-SEARCH-15, Open question 5

    def __init__(
        self,
        providers: Mapping[str, CoverProvider] | None = None,
        http: HttpFactory = httpx.AsyncClient,
        cache_root: Path | None = None,
        art_root: Path | None = None,
    ) -> None:
        self._providers = dict(providers) if providers is not None else None
        self._http = http
        self._cache_root = cache_root
        self._art_root = art_root

    def providers(self) -> dict[str, CoverProvider]:
        if self._providers is None:
            self._providers, _ = load_providers()
        return self._providers

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, CoversSearchParams)
        settings = ctx.profile_settings.covers if ctx.profile_settings else CoverSettings()
        providers = self.providers()
        unknown = [n for n in plan(settings, params.providers).order if n not in providers]
        if unknown:
            raise StepFailure(f"no cover provider named {', '.join(unknown)}")
        from rifferr.profile_store import art_store_path, covers_cache_path

        store = ArtStore(self._art_root or art_store_path(ctx.profile))
        cache = CoverCache(
            self._cache_root or covers_cache_path(),
            ttl=parse_duration(settings.cache_ttl),
            max_bytes=parse_size(settings.cache_max),
        )
        counts = {"chosen": 0, "skipped": 0, "review": 0, "none": 0}

        def secret(name: str) -> str | None:
            try:
                return ctx.secret(name).reveal()
            except StepFailure:
                return None

        async with self._http() as client:
            for album in ctx.albums():
                if ctx.cancelled:
                    break
                label = album_label(album)
                current = album.fields.get(ART_FIELD)
                if params.only_missing and _has_art(album, current, params, settings):
                    ctx.log(f"{label}: has art, skipped", target=album.id)
                    counts["skipped"] += 1
                    continue
                query = build_query(ctx, album, params.query)
                result = await search(
                    query,
                    settings,
                    providers,
                    client,
                    cache=cache,
                    only=params.providers,
                    refresh=params.refresh,
                    secret=secret,
                )
                _log_search(ctx, album, label, result)
                old = current if isinstance(current, str) else None
                if params.choose == "review":
                    self._review(ctx, album, label, query, result, old, "choose: review")
                    counts["review"] += 1
                    continue
                choice = await choose(
                    ctx, result, settings, providers, client, cache, params, album.id
                )
                if choice is not None:
                    sha, _ = store.put(choice.image.data)
                    info = choice.image.info
                    ctx.propose(
                        ChangeSetProposal(
                            changes=[ProposedFieldChange(field=ART_FIELD, new=sha, old=old)],
                            rationale=(
                                f"cover from {choice.candidate.provider} "
                                f"({info.width}x{info.height}, score {choice.score:.2f})"
                                + (
                                    f": {choice.candidate.source_url}"
                                    if choice.candidate.source_url
                                    else ""
                                )
                            ),
                        ),
                        current=TargetRef(scope="album", id=album.id),
                    )
                    ctx.log(
                        f"{label}: chose {choice.candidate.provider} {info.width}x{info.height}"
                        f" (score {choice.score:.2f})",
                        target=album.id,
                    )
                    counts["chosen"] += 1
                    continue
                counts["none"] += 1
                why = f"no cover scored {params.min_score:g} or more"
                if params.on_none == "review":
                    self._review(ctx, album, label, query, result, old, why)
                    counts["review"] += 1
                elif params.on_none == "fail":
                    ctx.fail_target(album.id, why)
                else:
                    ctx.log(f"{label}: {why}; skipped", target=album.id)
        return StepResult(outputs=dict(counts))

    @staticmethod
    def _review(
        ctx: StepContext,
        album: AlbumSnapshot,
        label: str,
        query: CoverQuery,
        result: SearchResult,
        old: str | None,
        reason: str,
    ) -> None:
        ctx.request_cover(
            CoverRequest(
                album_id=album.id,
                label=label,
                query=query,
                candidates=result.ranking.ranked[:MAX_KEPT],
                dropped=result.ranking.dropped,
                old_sha256=old,
                reason=reason,
            )
        )
        ctx.log(f"{label}: waiting for a cover choice in the review queue", target=album.id)


def album_label(album: AlbumSnapshot) -> str:
    artist = album.fields.get("albumartist") or album.fields.get("artist") or ""
    name = album.fields.get("album") or album.id
    return f"{artist} - {name}" if artist else str(name)


def _has_art(
    album: AlbumSnapshot,
    current: object,
    params: CoversSearchParams,
    settings: CoverSettings,
) -> bool:
    """COVERS-SEARCH-12: art the backend knows, or (``local_counts``) an image in the
    album folder that the ``local`` provider would offer."""
    if current:
        return True
    if not params.local_counts:
        return False
    local = settings.provider("local")
    patterns = local.settings.get("patterns") if local is not None else None
    return bool(local_images(album.path, patterns))


def build_query(
    ctx: StepContext, album: AlbumSnapshot, mapping: Mapping[QueryField, str]
) -> CoverQuery:
    """COVERS-SEARCH-2: each field from a backend template on the album."""
    try:
        return album_query(ctx.backend, album, mapping)
    except QueryTemplateError as exc:
        raise StepFailure(str(exc)) from None


class QueryTemplateError(Exception):
    """A query field's template failed on the album."""


def album_query(
    backend: LibraryBackend,
    album: AlbumSnapshot,
    mapping: Mapping[QueryField, str] | None = None,
) -> CoverQuery:
    """The cover query of ``album``: each field from a backend template (COVERS-SEARCH-2,
    COVER-PICKER-2). Raises ``QueryTemplateError``."""
    values: dict[str, object] = {"folder": album.path}
    for name, template in {**DEFAULT_QUERY, **(mapping or {})}.items():
        try:
            text = backend.evaluate(template, album).strip()
        except TemplateError as exc:
            raise QueryTemplateError(f"query.{name}: {exc}") from None
        if name == "year":
            values[name] = int(text) if text.isdigit() and int(text) > 0 else None
        elif name in ("artist", "album"):
            values[name] = text
        else:
            values[name] = text or None
    return CoverQuery.model_validate(values)


def _log_search(ctx: StepContext, album: AlbumSnapshot, label: str, result: SearchResult) -> None:
    for s in result.statuses:
        detail = f": {s.message}" if s.message else ""
        cached = " (cached)" if s.cached else ""
        stream = "error" if s.state == "failed" else "info"
        ctx.log(f"{label}: {s.name} {s.state}, {s.count} found{cached}{detail}", stream, album.id)
    for provider, reasons in result.ranking.dropped.items():
        text = ", ".join(f"{n} {r}" for r, n in sorted(reasons.items()))
        ctx.log(f"{label}: {provider}: dropped {text}", target=album.id)


async def choose(
    ctx: StepContext,
    result: SearchResult,
    settings: CoverSettings,
    providers: Mapping[str, CoverProvider],
    client: httpx.AsyncClient,
    cache: CoverCache,
    params: CoversSearchParams,
    album_id: str,
) -> Choice | None:
    """COVERS-SEARCH-13: the best candidate scoring at least ``min_score`` at its real size.

    Candidates are tried best first. One whose size the provider didn't give is
    downloaded to learn it and ranked again; at most ``max_tries`` downloads."""
    weights = plan(settings, params.providers).weights
    pool: list[tuple[float, int, RankedCandidate, Image | None]] = [
        (_upper(c, weights), i, c, None) for i, c in enumerate(result.ranking.ranked)
    ]
    tries = 0
    while pool:
        pool.sort(key=lambda e: (-e[0], e[1]))
        score, order, c, image = pool.pop(0)
        if score < params.min_score:
            return None  # sorted: nothing after it scores more
        if image is not None:
            return Choice(c, image, score)
        if tries >= params.max_tries:
            return None
        tries += 1
        try:
            got = await download(c, settings, providers, client, cache=cache)
        except DownloadError as exc:
            ctx.log(f"{c.provider}: {exc}", "error", album_id)
            continue
        real = round(
            weights.get(c.provider, 1.0)
            * c.match_confidence
            * size_score(got.info.width, got.info.height, settings),
            4,
        )
        pool.append((real, order, c, got))
    return None


def _upper(c: RankedCandidate, weights: Mapping[str, float]) -> float:
    """The score a candidate can reach: its own when its size is known, else as if its
    image were big enough (its download decides)."""
    if c.width is not None and c.height is not None:
        return c.score
    return round(weights.get(c.provider, 1.0) * c.match_confidence, 4)
