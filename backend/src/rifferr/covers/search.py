"""Running a cover search across providers, and downloading a chosen image
(S-COVERS-SEARCH COVERS-SEARCH-4 to -6, -9, -11, -13), and images the user adds
themselves (F-COVER-PICKER COVER-PICKER-18)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue

from rifferr.core.covers import (
    CoverCandidate,
    CoverQuery,
    CoverSettings,
    ImageInfo,
    ProviderSetting,
    RankedCandidate,
    Ranking,
    filter_reason,
    parse_size,
    rank,
    score_candidate,
    sha256_hex,
    sniff_image,
)
from rifferr.core.duration import parse_duration
from rifferr.core.profile import Redactor
from rifferr.core.profile import Secret as SecretValue
from rifferr.covers.base import MATCH_KEY, CoverProvider
from rifferr.covers.cache import CoverCache
from rifferr.covers.http import CoverHttp, CoverHttpError, throttle

log = logging.getLogger(__name__)

UPLOAD_PROVIDER = "upload"
"""Candidates the user added (COVER-PICKER-18). Not a searchable provider: a profile may
list it only to give it a weight."""
UPLOAD_SCHEME = "upload:"

ProviderState = Literal["searching", "done", "failed", "skipped"]


class ProviderStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    state: ProviderState
    count: int = 0
    cached: bool = False
    message: str | None = None


@dataclass
class SearchResult:
    ranking: Ranking
    statuses: list[ProviderStatus]
    candidates: list[CoverCandidate] = field(default_factory=list[CoverCandidate])


class DownloadError(Exception):
    """The image couldn't be fetched, or isn't one Rifferr can use."""


@dataclass
class Plan:
    """Which providers a search uses, in order, with their weights and settings."""

    order: list[str]
    weights: dict[str, float]
    settings: dict[str, dict[str, JsonValue]]


def plan(settings: CoverSettings, only: Sequence[str] | None = None) -> Plan:
    """The profile's enabled providers, or ``only`` (a step's or a search's override,
    in its order, enabled or not; weights still come from the profile)."""
    if only is None:
        chosen = [p for p in settings.providers if p.enabled]
    else:
        chosen = [settings.provider(n) or ProviderSetting(name=n) for n in dict.fromkeys(only)]
    chosen = [p for p in chosen if p.name != UPLOAD_PROVIDER]
    return Plan(
        order=[p.name for p in chosen],
        weights={p.name: p.weight for p in chosen},
        settings={p.name: dict(p.settings) for p in chosen},
    )


def http_for(
    provider: CoverProvider, client: httpx.AsyncClient, settings: CoverSettings
) -> CoverHttp:
    return CoverHttp(
        client,
        provider.hosts,
        max_bytes=parse_size(settings.max_bytes),
        timeout=parse_duration(settings.timeout).total_seconds(),
        contact=settings.contact,
        throttle=throttle(provider.name),
        min_interval=float(getattr(provider, "min_interval", 0) or 0),
        secret_params=getattr(provider, "secret_params", frozenset[str]()),
    )


Secret = Callable[[str], str | None]
OnCandidate = Callable[[CoverCandidate], Awaitable[None] | None]
OnProvider = Callable[[ProviderStatus], Awaitable[None] | None]


async def _call(fn: Callable[[Any], Awaitable[None] | None] | None, value: Any) -> None:
    if fn is None:
        return
    out = fn(value)
    if out is not None:
        await out


async def search(
    query: CoverQuery,
    settings: CoverSettings,
    providers: Mapping[str, CoverProvider],
    client: httpx.AsyncClient,
    *,
    cache: CoverCache | None = None,
    only: Sequence[str] | None = None,
    refresh: bool = False,
    secret: Secret | None = None,
    on_candidate: OnCandidate | None = None,
    on_provider: OnProvider | None = None,
) -> SearchResult:
    """Ask every planned provider at once; emit candidates as they come
    (COVERS-SEARCH-6). A failing provider ends only its own results (COVERS-SEARCH-5)."""
    p = plan(settings, only)
    query_key = query.key()
    found: list[CoverCandidate] = []
    statuses: dict[str, ProviderStatus] = {}
    match: dict[str, JsonValue] = {
        "normalize": [r.model_dump() for r in settings.normalize],
        "year_penalty": settings.year_penalty,
    }

    async def report(status: ProviderStatus) -> None:
        statuses[status.name] = status
        await _call(on_provider, status)

    async def one(name: str) -> None:
        provider = providers.get(name)
        if provider is None:
            await report(ProviderStatus(name=name, state="failed", message="no such provider"))
            return
        redactor = Redactor([])
        own: dict[str, JsonValue] = {**p.settings.get(name, {}), MATCH_KEY: match}
        if provider.needs:
            secret_name = own.get("secret")
            token = secret(secret_name) if secret and isinstance(secret_name, str) else None
            if not token:
                await report(
                    ProviderStatus(name=name, state="skipped", message="its secret isn't set")
                )
                return
            own["token"] = token
            redactor = Redactor([SecretValue(token)])
        cached = None if refresh or cache is None else cache.results(name, query_key)
        if cached is not None:
            for c in cached:
                found.append(c)
                await _call(on_candidate, c)
            await report(ProviderStatus(name=name, state="done", count=len(cached), cached=True))
            return
        await report(ProviderStatus(name=name, state="searching"))
        mine: list[CoverCandidate] = []
        try:
            async for c in provider.search(query, own, http_for(provider, client, settings)):
                if c.provider != name:
                    c = c.model_copy(update={"provider": name})
                mine.append(c)
                found.append(c)
                await _call(on_candidate, c)
        except asyncio.CancelledError:
            raise
        except CoverHttpError as exc:
            await report(
                ProviderStatus(
                    name=name, state="failed", count=len(mine), message=redactor.redact(str(exc))
                )
            )
            return
        except Exception as exc:
            message = redactor.redact(f"{type(exc).__name__}: {exc}")
            log.warning("cover provider %s failed: %s", name, message)
            await report(
                ProviderStatus(name=name, state="failed", count=len(mine), message=message)
            )
            return
        if cache is not None:
            cache.store_results(name, query_key, mine)
        await report(ProviderStatus(name=name, state="done", count=len(mine)))

    async with asyncio.TaskGroup() as group:
        for name in p.order:
            group.create_task(one(name))
    ranking = rank(found, settings, p.weights, p.order)
    return SearchResult(
        ranking=ranking, statuses=[statuses[n] for n in p.order if n in statuses], candidates=found
    )


# -- downloading (COVERS-SEARCH-13) -----------------------------------------------------


@dataclass(frozen=True)
class Image:
    data: bytes
    info: ImageInfo


async def download(
    candidate: CoverCandidate,
    settings: CoverSettings,
    providers: Mapping[str, CoverProvider],
    client: httpx.AsyncClient,
    *,
    cache: CoverCache | None = None,
    thumb: bool = False,
    check: bool = True,
) -> Image:
    """The candidate's image (or thumbnail), from the cache when there. It must be a JPEG
    or PNG by its magic bytes, and, with ``check``, pass the hard filters at its real
    size. Raises ``DownloadError``."""
    limit = parse_size(settings.max_bytes)
    if candidate.path is not None and not thumb:
        data = _read_local(Path(candidate.path), limit)
    else:
        url = candidate.thumb_url if thumb else candidate.image_url
        if url is None:
            raise DownloadError("the candidate has no image to fetch")
        data = cache.image(url) if cache is not None else None
        if data is None and url.startswith(UPLOAD_SCHEME):
            raise DownloadError("the added image is no longer cached; add it again")
        if data is None:
            provider = providers.get(candidate.provider)
            if provider is None:
                raise DownloadError(f"no cover provider named {candidate.provider}")
            try:
                data = await http_for(provider, client, settings).get_bytes(url)
            except CoverHttpError as exc:
                raise DownloadError(f"{candidate.provider}: {exc}") from None
            if sniff_image(data) is not None and cache is not None:
                cache.store_image(url, data)
    info = sniff_image(data)
    if info is None:
        raise DownloadError("not a JPEG or PNG image")
    if check and not thumb:
        reason = filter_reason(info.width, info.height, settings)
        if reason is not None:
            raise DownloadError(
                f"the image is {info.width}x{info.height}: "
                + ("too small" if reason == "small" else "not square enough")
            )
    return Image(data, info)


def _read_local(path: Path, limit: int) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise DownloadError(f"{path.name} isn't a file")
        if path.stat().st_size > limit:
            raise DownloadError(f"{path.name} is larger than {limit} bytes")
        return path.read_bytes()
    except OSError as exc:
        raise DownloadError(f"can't read {path.name}: {exc.strerror}") from None


# -- the user's own images (COVER-PICKER-18) ---------------------------------------------


def accept_upload(data: bytes, settings: CoverSettings, cache: CoverCache) -> RankedCandidate:
    """Check an image the user added and keep it in the cache as a candidate of provider
    ``upload``. Images the hard filters would drop are kept too, marked ``filtered``.
    Raises ``DownloadError``."""
    limit = parse_size(settings.max_bytes)
    if len(data) > limit:
        raise DownloadError(f"the image is larger than {limit} bytes")
    info = sniff_image(data)
    if info is None:
        raise DownloadError("not a JPEG or PNG image")
    url = UPLOAD_SCHEME + sha256_hex(data)
    candidate = CoverCandidate.make(
        UPLOAD_PROVIDER,
        image_url=url,
        title="Added image",
        width=info.width,
        height=info.height,
        match_confidence=1.0,
    )
    cache.store_image(url, data)
    cache.remember(candidate)
    own = settings.provider(UPLOAD_PROVIDER)
    return score_candidate(candidate, settings, own.weight if own is not None else 1.0)
