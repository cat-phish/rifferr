"""Request security for the HTTP server (F-SECURITY), as plain ASGI middleware.

No framework dependency: F-API mounts ``secure(app, config)`` around its app.
Order, outermost first: security headers, Host allowlist, Origin / fetch-site,
JSON-only, token auth (SECURITY-2..7, -13). Everything except auth stays on
with ``--no-auth``.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import secrets
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

EXCHANGE_PATH = "/auth/exchange"
HEALTH_PATH = "/healthz"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_BYTES = 32
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
DOCS_HINT = "see specs/features/F-SECURITY.md"


class BindRefusedError(Exception):
    """Refusing to listen on a non-loopback address without real auth (SECURITY-9)."""


@dataclass(frozen=True)
class SecurityConfig:
    port: int
    token: str
    no_auth: bool = False
    extra_hosts: tuple[str, ...] = ()
    """Extra ``Host`` values to accept (e.g. a reverse proxy name), with port if any."""
    exempt_prefixes: tuple[str, ...] = ()
    """Path prefixes exempt from the Origin and JSON checks (webhooks, M2)."""
    public_paths: frozenset[str] = field(default_factory=lambda: frozenset({HEALTH_PATH}))

    @property
    def cookie_name(self) -> str:
        return f"rifferr_token_{self.port}"

    def allowed_hosts(self) -> frozenset[str]:
        p = self.port
        return frozenset(
            {
                f"127.0.0.1:{p}",
                f"localhost:{p}",
                f"[::1]:{p}",
                *(h.lower() for h in self.extra_hosts),
            }
        )

    def allowed_origins(self) -> frozenset[str]:
        origins = {f"http://{h}" for h in self.allowed_hosts()}
        origins |= {f"https://{h}" for h in self.extra_hosts}
        return frozenset(o.lower() for o in origins)

    def token_ok(self, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(
            candidate.encode(), self.token.encode()
        )


# -- tokens and binding ---------------------------------------------------------


def generate_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_file_path(port: int, env: MutableMapping[str, str] | None = None) -> Path:
    """``$XDG_RUNTIME_DIR/rifferr/<port>.token``, else under the config dir (SECURITY-8)."""
    env = os.environ if env is None else env
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime:
        base = Path(runtime) / "rifferr"
    else:
        from rifferr.profile_store import config_root

        base = config_root(env) / "run"
    return base / f"{port}.token"


def write_token_file(port: int, token: str, env: MutableMapping[str, str] | None = None) -> Path:
    path = token_file_path(port, env)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def read_token_file(port: int, env: MutableMapping[str, str] | None = None) -> str | None:
    try:
        return token_file_path(port, env).read_text().strip() or None
    except FileNotFoundError:
        return None


def remove_token_file(port: int, env: MutableMapping[str, str] | None = None) -> None:
    token_file_path(port, env).unlink(missing_ok=True)


def is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def check_bind(host: str, *, real_auth: bool = False) -> None:
    """SECURITY-1, -9. M0 has no real auth, so only loopback binds are allowed."""
    if not is_loopback(host) and not real_auth:
        raise BindRefusedError(f"binding to {host} requires authentication; {DOCS_HINT}")


# -- responses --------------------------------------------------------------------


async def _respond(
    send: Send,
    status: int,
    message: str,
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _exempt(scope: Scope, config: SecurityConfig) -> bool:
    path: str = scope.get("path", "")
    return any(path.startswith(p) for p in config.exempt_prefixes)


def _bearer(scope: Scope) -> str | None:
    auth = _header(scope, b"authorization")
    if auth and auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return None


def _cookie(scope: Scope, name: str) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return None
    morsel = jar.get(name)
    return morsel.value if morsel is not None else None


# -- middleware --------------------------------------------------------------------


class SecurityHeaders:
    """SECURITY-13 on every response; strips CORS headers (SECURITY-5)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in SECURITY_HEADERS}
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if not k.lower().startswith(b"access-control-") and k.lower() not in names
                ]
                message["headers"] = [*headers, *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, receive, wrapped)


class HostAllowlist:
    """SECURITY-2: defeats DNS rebinding."""

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        self.app = app
        self.allowed = config.allowed_hosts()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            host = (_header(scope, b"host") or "").lower()
            if host not in self.allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await _respond(send, 400, "unexpected Host header")
                return
        await self.app(scope, receive, send)


class OriginCheck:
    """SECURITY-3: blocks cross-site requests from other pages in the browser."""

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        self.app = app
        self.config = config
        self.origins = config.allowed_origins()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or _exempt(scope, self.config):
            await self.app(scope, receive, send)
            return
        origin = _header(scope, b"origin")
        if origin is not None and origin.lower() not in self.origins:
            await _respond(send, 403, "cross-origin request refused")
            return
        mutating = scope["type"] == "http" and scope.get("method", "GET").upper() in MUTATING
        if mutating and origin is None:
            same_site = (_header(scope, b"sec-fetch-site") or "").lower() == "same-origin"
            if not same_site and not self.config.token_ok(_bearer(scope)):
                await _respond(send, 403, "request without Origin refused")
                return
        await self.app(scope, receive, send)


class JsonOnly:
    """SECURITY-4: mutating requests must be JSON (no form posts from other sites)."""

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method", "GET").upper() in MUTATING
            and not _exempt(scope, self.config)
        ):
            media = (_header(scope, b"content-type") or "").split(";", 1)[0].strip().lower()
            if media != "application/json":
                await _respond(send, 415, "only application/json is accepted")
                return
        await self.app(scope, receive, send)


class TokenAuth:
    """SECURITY-6, -7: local token via cookie or bearer; the token exchange route."""

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope["type"] == "http" and path == EXCHANGE_PATH:
            await self._exchange(scope, send)
            return
        if self.config.no_auth or path in self.config.public_paths:
            await self.app(scope, receive, send)
            return
        if self.config.token_ok(_cookie(scope, self.config.cookie_name)) or self.config.token_ok(
            _bearer(scope)
        ):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await _respond(send, 401, "authentication required")

    async def _exchange(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (query.get("token") or [None])[0]
        headers: list[tuple[bytes, bytes]] = [(b"location", b"/"), (b"cache-control", b"no-store")]
        if not self.config.no_auth:
            if not self.config.token_ok(token):
                await _respond(send, 403, "invalid or expired token")
                return
            cookie = (
                f"{self.config.cookie_name}={self.config.token}; HttpOnly; SameSite=Strict; Path=/"
            )
            headers.append((b"set-cookie", cookie.encode()))
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


def secure(app: ASGIApp, config: SecurityConfig) -> ASGIApp:
    """Wrap ``app`` in the full F-SECURITY stack."""
    inner: ASGIApp = TokenAuth(app, config)
    inner = JsonOnly(inner, config)
    inner = OriginCheck(inner, config)
    inner = HostAllowlist(inner, config)
    return SecurityHeaders(inner)
