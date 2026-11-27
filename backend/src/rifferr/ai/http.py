"""The one HTTP client AI providers may use (AI-CORE-4).

It talks to exactly one host, refuses redirects, caps the response size and puts one
timeout around the whole call. Errors say whether they are worth retrying (AI-CORE-14),
and never carry the API key.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from rifferr.ai.base import AIError
from rifferr.core.ai import is_private_host
from rifferr.core.profile import Redactor

ERROR_SNIPPET = 300


@dataclass(frozen=True)
class AIReply:
    status: int
    headers: Mapping[str, str]
    text: str

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except ValueError:
            raise AIError("the server's answer is not JSON", retryable=True) from None

    @property
    def error_text(self) -> str:
        """A short piece of the body, for an error message."""
        return " ".join(self.text.split())[:ERROR_SNIPPET]


def parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    """Seconds from a ``Retry-After`` header (seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


class AIHttp:
    def __init__(
        self,
        client: httpx.AsyncClient,
        host: str,
        *,
        timeout: float,
        max_bytes: int,
        redactor: Redactor | None = None,
    ) -> None:
        self.client = client
        self.host = host.lower()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.redactor = redactor or Redactor([])

    def check_url(self, url: str) -> None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host != self.host:
            raise AIError(f"host {host or '?'} is not this provider's host", retryable=False)
        if parts.scheme == "https":
            return
        if parts.scheme == "http" and is_private_host(host):
            return
        raise AIError("only https:// is allowed for a non-local host", retryable=False)

    async def post_json(
        self, url: str, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> AIReply:
        """POST ``body`` as JSON. Raises ``AIError`` for transport problems, redirects,
        429 and 5xx; returns the reply for anything else, so a provider can read a 4xx."""
        self.check_url(url)
        try:
            async with asyncio.timeout(self.timeout):
                return await self._post(url, body, headers or {})
        except TimeoutError:
            raise AIError(f"timed out after {self.timeout:g}s", retryable=True) from None
        except httpx.HTTPError as exc:
            message = self.redactor.redact(f"request failed: {type(exc).__name__}: {exc}")
            raise AIError(message, retryable=True) from None

    async def _post(self, url: str, body: Mapping[str, Any], headers: Mapping[str, str]) -> AIReply:
        request = self.client.build_request("POST", url, json=dict(body), headers=dict(headers))
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            if response.is_redirect:
                raise AIError(
                    f"the server answered with a redirect (HTTP {response.status_code}); "
                    "check base_url",
                    retryable=False,
                )
            if response.status_code == 429 or response.status_code >= 500:
                raise AIError(
                    f"HTTP {response.status_code}",
                    retryable=True,
                    retry_after=parse_retry_after(response.headers.get("retry-after")),
                )
            raw = await self._read(response)
        finally:
            await response.aclose()
        text = self.redactor.redact(raw.decode("utf-8", errors="replace"))
        return AIReply(status=response.status_code, headers=dict(response.headers), text=text)

    async def _read(self, response: httpx.Response) -> bytes:
        declared = response.headers.get("content-length")
        too_big = AIError(f"answer is larger than {self.max_bytes} bytes", retryable=False)
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise too_big
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > self.max_bytes:
                raise too_big
        return bytes(body)

    def fail(self, reply: AIReply) -> AIError:
        """The error for a 4xx reply: not retried (AI-CORE-14)."""
        return AIError(f"HTTP {reply.status}: {reply.error_text}", retryable=False)
