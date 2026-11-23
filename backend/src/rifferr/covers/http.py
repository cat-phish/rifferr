"""The HTTP client cover providers get (S-COVERS-SEARCH COVERS-SEARCH-5).

Only ``https`` to the provider's declared hosts, redirects included; a fixed User-Agent;
a time limit per request and a size limit per body. Providers never make requests any
other way.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx

from rifferr import __version__

PROJECT_URL: Final = "https://github.com/cat-phish/rifferr"
MAX_REDIRECTS: Final = 5
MAX_RETRY_AFTER: Final = 60.0
"""The longest a provider's ``Retry-After`` may hold its next request (COVERS-SEARCH-26)."""
REDACTED: Final = "REDACTED"


class Throttle:
    """Spaces one provider's requests (COVERS-SEARCH-26). Shared by every search in this
    process; each process (the server, a profile's worker) keeps its own."""

    def __init__(self) -> None:
        self.next_at = 0.0

    async def wait(self, interval: float) -> None:
        now = time.monotonic()
        slot = max(now, self.next_at)
        self.next_at = max(self.next_at, slot + interval)
        if slot > now:
            await asyncio.sleep(slot - now)

    def back_off(self, seconds: float) -> None:
        self.next_at = max(self.next_at, time.monotonic() + min(seconds, MAX_RETRY_AFTER))


_THROTTLES: dict[str, Throttle] = {}


def throttle(name: str) -> Throttle:
    """The throttle of the provider called ``name``."""
    return _THROTTLES.setdefault(name, Throttle())


def retry_after(value: str | None) -> float | None:
    """Seconds from a ``Retry-After`` header given in seconds; dates are ignored."""
    if value and value.strip().isdigit():
        return float(value.strip())
    return None


def redact_url(url: str, secret_params: Iterable[str]) -> str:
    """``url`` with the values of ``secret_params`` replaced (COVERS-SEARCH-27)."""
    names = set(secret_params)
    if not names:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    query = [(k, REDACTED if k in names else v) for k, v in parse_qsl(parts.query, True)]
    return urlunsplit(parts._replace(query=urlencode(query)))


@dataclass(frozen=True)
class Reply:
    status: int
    url: str
    content: bytes
    content_type: str = ""
    retry_after: str | None = None

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
        throttle: Throttle | None = None,
        min_interval: float = 0.0,
        secret_params: Iterable[str] = (),
    ) -> None:
        self._client = client
        self.hosts = frozenset(hosts)
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._headers = {"User-Agent": user_agent(contact)}
        self._throttle = throttle
        self.min_interval = min_interval
        self.secret_params = frozenset(secret_params)

    def check_url(self, url: str) -> None:
        try:
            parts = urlsplit(url)
        except ValueError:
            raise CoverHttpError("the URL can't be parsed") from None
        if parts.scheme != "https":
            raise CoverHttpError(f"only https is allowed: {parts.scheme or 'no'} scheme")
        if not parts.hostname or not host_allowed(parts.hostname, self.hosts):
            raise CoverHttpError(f"host {parts.hostname!r} isn't one this provider may use")

    async def get(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        """GET with redirects checked hop by hop; the body is read up to ``max_bytes``.
        ``timeout`` is also the limit for the whole call, every hop and the body included.
        ``headers`` (e.g. a provider's auth) go only to ``url``'s own host. The wait for the
        provider's throttle comes first and isn't part of the timeout."""
        if self._throttle is not None:
            await self._throttle.wait(self.min_interval)
        try:
            async with asyncio.timeout(self.timeout):
                reply = await self._get(url, params, headers or {})
        except TimeoutError:
            raise CoverHttpError(f"timed out after {self.timeout:g}s") from None
        if reply.status == 429:
            if self._throttle is not None:
                wait = retry_after(reply.retry_after)
                self._throttle.back_off(wait if wait is not None else self.min_interval)
            raise CoverHttpError("rate limited")
        return reply

    async def _get(
        self, url: str, params: Mapping[str, str] | None, extra: Mapping[str, str]
    ) -> Reply:
        current = url
        query: Mapping[str, str] | None = params
        origin = urlsplit(url).hostname
        for _ in range(MAX_REDIRECTS + 1):
            self.check_url(current)
            headers = dict(self._headers)
            if extra and urlsplit(current).hostname == origin:
                headers.update(extra)
            try:
                request = self._client.build_request(
                    "GET", current, params=query, headers=headers, timeout=self.timeout
                )
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException:
                raise CoverHttpError(f"timed out after {self.timeout:g}s") from None
            except httpx.InvalidURL:
                raise CoverHttpError("the URL can't be used") from None
            except httpx.HTTPError as exc:
                raise CoverHttpError(f"request failed: {type(exc).__name__}") from None
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise CoverHttpError("redirect without a location")
                    try:
                        current = urljoin(str(response.url), location)
                    except ValueError:
                        raise CoverHttpError("redirect to a URL that can't be parsed") from None
                    query = None
                    continue
                return Reply(
                    status=response.status_code,
                    url=redact_url(str(response.url), self.secret_params),
                    content=b"" if response.status_code == 429 else await self._read(response),
                    content_type=response.headers.get("content-type", ""),
                    retry_after=response.headers.get("retry-after"),
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

    async def get_json(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        response = await self.get(url, params, headers)
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
