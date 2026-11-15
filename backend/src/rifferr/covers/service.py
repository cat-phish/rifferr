"""Cover search in the server (S-COVERS-SEARCH COVERS-SEARCH-19 to -21): providers,
one shared HTTP client, the cache and art stores, and the searches running now."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

import httpx

from rifferr.artstore import ArtStore
from rifferr.core.covers import CoverSettings, parse_size
from rifferr.core.duration import parse_duration
from rifferr.covers.base import CoverProvider
from rifferr.covers.cache import CoverCache
from rifferr.covers.registry import load_providers


class CoverService:
    def __init__(
        self,
        providers: dict[str, CoverProvider] | None = None,
        *,
        http: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
        cache_root: Path | None = None,
        art_root: Callable[[str], Path] | None = None,
    ) -> None:
        self._providers = providers
        self._http = http
        self._client: httpx.AsyncClient | None = None
        self._cache_root = cache_root
        self._art_root = art_root
        self.searches: dict[str, asyncio.Task[None]] = {}

    @property
    def providers(self) -> dict[str, CoverProvider]:
        if self._providers is None:
            self._providers, _ = load_providers()
        return self._providers

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._http()
        return self._client

    def cache(self, settings: CoverSettings) -> CoverCache:
        from rifferr.profile_store import covers_cache_path

        return CoverCache(
            self._cache_root or covers_cache_path(),
            ttl=parse_duration(settings.cache_ttl),
            max_bytes=parse_size(settings.cache_max),
        )

    def store(self, profile: str) -> ArtStore:
        from rifferr.profile_store import art_store_path

        return ArtStore(self._art_root(profile) if self._art_root else art_store_path(profile))

    async def close(self) -> None:
        for task in list(self.searches.values()):
            task.cancel()
        for task in list(self.searches.values()):
            with contextlib.suppress(BaseException):  # shutting down
                await task
        self.searches.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
