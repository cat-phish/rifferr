"""The HTTP client cover providers get (S-COVERS-SEARCH COVERS-SEARCH-5).

Only ``https`` to the provider's declared hosts, redirects included; a fixed User-Agent;
a time limit per request and a size limit per body. Providers never make requests any
other way.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx

from rifferr import __version__

PROJECT_URL: Final = "https://github.com/cat-phish/rifferr"
MAX_REDIRECTS: Final = 5


@dataclass(frozen=True)
class Reply:
    status: int
    url: str
    content: bytes
    content_type: str = ""

    def json(self) -> Any:
        return json.loads(self.content)


class CoverHttpError(Exception):
    """A refused or failed request; the message never holds secrets."""


def user_agent(contact: str | None = None) -> str:
    extra = f"; {contact}" if contact else ""
    return f"Rifferr/{__version__} (+{PROJECT_URL}{extra})"


def host_allowed(host: str, hosts: Iterable[str]) -> bool:
    """``hosts`` entries are exact names, or ``.example.org`` for any subdomain."""
    host = host.lower().rstrip(".")
    for allowed in hosts:
        allowed = allowed.lower()
        if allowed.startswith("."):
            if host.endswith(allowed):
                return True
        elif host == allowed:
            return True
    return False


class CoverHttp:
    """One provider's view of the network."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        hosts: Iterable[str],
        *,
        max_bytes: int,
        timeout: float,
        contact: str | None = None,
    ) -> None:
        self._client = client
        self.hosts = frozenset(hosts)
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._headers = {"User-Agent": user_agent(contact)}

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise CoverHttpError(f"only https is allowed: {parts.scheme or 'no'} scheme")
        if not parts.hostname or not host_allowed(parts.hostname, self.hosts):
            raise CoverHttpError(f"host {parts.hostname!r} isn't one this provider may use")

    async def get(self, url: str, params: Mapping[str, str] | None = None) -> Reply:
        """GET with redirects checked hop by hop; the body is read up to ``max_bytes``."""
        current = url
        query: Mapping[str, str] | None = params
        for _ in range(MAX_REDIRECTS + 1):
            self.check_url(current)
            try:
                request = self._client.build_request(
                    "GET", current, params=query, headers=self._headers, timeout=self.timeout
                )
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException:
                raise CoverHttpError(f"timed out after {self.timeout:g}s") from None
            except httpx.HTTPError as exc:
                raise CoverHttpError(f"request failed: {type(exc).__name__}") from None
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise CoverHttpError("redirect without a location")
                    current = urljoin(str(response.url), location)
                    query = None
                    continue
                return Reply(
                    status=response.status_code,
                    url=str(response.url),
                    content=await self._read(response),
                    content_type=response.headers.get("content-type", ""),
                )
            finally:
                await response.aclose()
        raise CoverHttpError("too many redirects")

    async def _read(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise CoverHttpError(f"response is larger than {self.max_bytes} bytes")
        body = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise CoverHttpError(f"response is larger than {self.max_bytes} bytes")
        except httpx.TimeoutException:
            raise CoverHttpError(f"timed out after {self.timeout:g}s") from None
        return bytes(body)

    async def get_json(self, url: str, params: Mapping[str, str] | None = None) -> Any:
        response = await self.get(url, params)
        if response.status == 404:
            return None
        if response.status >= 400:
            raise CoverHttpError(f"HTTP {response.status}")
        try:
            return response.json()
        except ValueError:
            raise CoverHttpError("the reply isn't JSON") from None

    async def get_bytes(self, url: str) -> bytes:
        response = await self.get(url)
        if response.status >= 400:
            raise CoverHttpError(f"HTTP {response.status}")
        return response.content
