"""Request security for the HTTP server (F-SECURITY), as plain ASGI middleware.

No framework dependency: F-API mounts ``secure(app, config)`` around its app.
Order, outermost first: security headers, Host allowlist, Origin / fetch-site,
JSON-only, token auth (SECURITY-2..7, -13). Everything except auth stays on
with ``--no-auth``.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

EXCHANGE_PATH = "/auth/exchange"
SIGN_IN_LINK_PATH = "/auth/sign-in-link"
HEALTH_PATH = "/healthz"
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_BYTES = 32
FRAME_ANCESTORS = b"frame-ancestors 'none'"
# SEC-45: the SPA is same-origin only; Mantine needs inline styles, previews use blob:/data:.
CSP_DIRECTIVES: tuple[bytes, ...] = (
    b"default-src 'self'",
    b"script-src 'self'",
    b"style-src 'self' 'unsafe-inline'",
    b"img-src 'self' data: blob:",
    b"connect-src 'self'",
    b"object-src 'none'",
    b"base-uri 'none'",
    b"form-action 'self'",
    FRAME_ANCESTORS,
)
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", b"; ".join(CSP_DIRECTIVES)),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
DOCS_HINT = "see specs/features/F-SECURITY.md"
SIGNED_OUT_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Rifferr - signed out</title>
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto;
 padding: 0 1rem">
<h1>Signed out</h1>
<p>Open the sign-in link that <code>rifferr serve</code> printed in its terminal.</p>
<p>Link expired, or running Rifferr as a service? Run <code>rifferr sign-in</code> on the
server for a new one-time sign-in link.</p>
</body></html>
"""


class BindRefusedError(Exception):
    """Refusing to listen on a non-loopback address without real auth (SECURITY-9)."""


_HOST_NAME = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
)


def normalize_host(value: str) -> str:
    """An extra allowed ``Host`` (``name``, ``name:port``, ``[v6]:port``), lower-cased.

    Only a plain host name or IP address with an optional port: no scheme, path,
    wildcard or user info (SEC-46). Raises ``ValueError``.
    """
    host = value.strip().lower()
    name, port = host, None
    if host.startswith("["):
        name, sep, rest = host[1:].partition("]")
        if not sep or (rest and not rest.startswith(":")):
            raise ValueError(f"not a host name: {value!r}")
        port = rest[1:] if rest else None
        try:
            ipaddress.IPv6Address(name)
        except ValueError:
            raise ValueError(f"not a host name: {value!r}") from None
    else:
        if host.count(":") == 1:
            name, port = host.split(":")
        if not _HOST_NAME.fullmatch(name) or len(name) > 253:
            raise ValueError(f"not a host name: {value!r}")
    if port is not None and (not port.isdigit() or not 0 < int(port) < 65536):
        raise ValueError(f"not a valid port in {value!r}")
    return host


class Sessions:
    """Browser sessions and one-time sign-in codes, in server memory only (SEC-42, -43).

    The cookie holds a session id, never the local token, so a leaked cookie is not a
    bearer token and ends at restart. Sign-in links carry a code that works once and
    for ``code_ttl`` seconds. Only SHA-256 digests are kept, so lookups don't compare
    secrets byte by byte.
    """

    def __init__(
        self,
        *,
        code_ttl: float = 15 * 60,
        max_sessions: int = 256,
        max_codes: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.code_ttl = code_ttl
        self.max_sessions = max_sessions
        self.max_codes = max_codes
        self.clock = clock
        self._codes: OrderedDict[bytes, float] = OrderedDict()
        self._sessions: OrderedDict[bytes, None] = OrderedDict()

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode()).digest()

    @staticmethod
    def _add(store: OrderedDict[bytes, Any], key: bytes, value: Any, cap: int) -> None:
        store[key] = value
        while len(store) > cap:
            store.popitem(last=False)  # drop the oldest

    def sign_in_code(self) -> str:
        code = secrets.token_urlsafe(TOKEN_BYTES)
        self._add(self._codes, self._digest(code), self.clock() + self.code_ttl, self.max_codes)
        return code

    def redeem(self, code: str | None) -> bool:
        """True once for a live code; the code is gone afterwards."""
        if not code:
            return False
        expires = self._codes.pop(self._digest(code), None)
        return expires is not None and self.clock() < expires

    def new_session(self) -> str:
        sid = secrets.token_urlsafe(TOKEN_BYTES)
        self._add(self._sessions, self._digest(sid), None, self.max_sessions)
        return sid

    def session_ok(self, sid: str | None) -> bool:
        return bool(sid) and self._digest(sid or "") in self._sessions


@dataclass(frozen=True)
class SecurityConfig:
    port: int
    token: str
    no_auth: bool = False
    extra_hosts: tuple[str, ...] = ()
    """Extra ``Host`` values to accept (e.g. a reverse proxy name), with port if any."""
    dev_origins: tuple[str, ...] = ()
    """Extra allowed origins for ``--dev`` only: the Vite dev server (API-13)."""
    exempt_prefixes: tuple[str, ...] = ()
    """Path prefixes exempt from the Origin and JSON checks (webhooks, M2)."""
    public_paths: frozenset[str] = field(default_factory=lambda: frozenset({HEALTH_PATH}))
    own_auth_prefixes: tuple[str, ...] = ()
    """Path prefixes whose routes authenticate requests themselves, instead of with the
    local token (webhooks with their own secrets, TRIGGERS-17). Still behind the Host
    allowlist."""
    sessions: Sessions = field(default_factory=Sessions, compare=False)
    """Browser sessions and sign-in codes (SEC-42, -43). Shared, mutable server state."""

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
        origins |= set(self.dev_origins)
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
    code: str,
    message: str,
    headers: Iterable[tuple[bytes, bytes]] = (),
) -> None:
    """The API error envelope (API-11)."""
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
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


async def _signed_out(send: Send) -> None:
    """SECURITY-7: the same 401, as a page with no app data."""
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(SIGNED_OUT_PAGE)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": SIGNED_OUT_PAGE})


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _exempt(scope: Scope, config: SecurityConfig) -> bool:
    path: str = scope.get("path", "")
    return any(path.startswith(p) for p in config.exempt_prefixes)


def _api_path(scope: Scope) -> bool:
    path: str = scope.get("path", "")
    return path.startswith("/api/") or path == "/openapi.json"


def _bearer(scope: Scope) -> str | None:
    auth = _header(scope, b"authorization")
    if auth and auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return None


def _cookies(scope: Scope, name: str) -> list[str]:
    """Every value sent for cookie ``name``, parsed by hand (SEC-42): one malformed or
    duplicate cookie, e.g. set by another app on the same host name, must not hide ours."""
    values: list[str] = []
    for key, raw in scope.get("headers", []):
        if key.lower() != b"cookie":
            continue
        for pair in raw.decode("latin-1").split(";"):
            k, sep, v = pair.strip().partition("=")
            if sep and k.strip() == name:
                values.append(v.strip().strip('"'))
    return values


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
                given = message.get("headers", [])
                # A route's own, stricter CSP (e.g. images, COVERS-SEARCH-20) is kept and
                # gets frame-ancestors added; it can never drop the fixed headers.
                csp = [v for k, v in given if k.lower() == b"content-security-policy"]
                headers = [
                    (k, v)
                    for k, v in given
                    if not k.lower().startswith(b"access-control-") and k.lower() not in names
                ]
                fixed = [
                    (
                        k,
                        csp[0] + b"; " + FRAME_ANCESTORS
                        if k == b"content-security-policy" and csp
                        else v,
                    )
                    for k, v in SECURITY_HEADERS
                ]
                message["headers"] = [*headers, *fixed]
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
                await _respond(send, 400, "host_not_allowed", "unexpected Host header")
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
            await _respond(send, 403, "cross_origin", "cross-origin request refused")
            return
        if scope["type"] == "http" and _api_path(scope):
            # SEC-41: browsers label cross-site/same-site fetches; non-browser clients send none.
            site = (_header(scope, b"sec-fetch-site") or "same-origin").lower()
            if site not in ("same-origin", "none"):
                await _respond(send, 403, "cross_origin", "cross-site request refused")
                return
        mutating = scope["type"] == "http" and scope.get("method", "GET").upper() in MUTATING
        if mutating and origin is None:
            same_site = (_header(scope, b"sec-fetch-site") or "").lower() == "same-origin"
            if not same_site and not self.config.token_ok(_bearer(scope)):
                await _respond(send, 403, "cross_origin", "request without Origin refused")
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
                await _respond(
                    send, 415, "unsupported_media_type", "only application/json is accepted"
                )
                return
        await self.app(scope, receive, send)


class TokenAuth:
    """SECURITY-6, -7: session cookie or local token as bearer; the sign-in routes."""

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
        if scope["type"] == "http" and path == SIGN_IN_LINK_PATH:
            await self._sign_in_link(scope, send)
            return
        if (
            self.config.no_auth
            or path in self.config.public_paths
            or any(path.startswith(p) for p in self.config.own_auth_prefixes)
        ):
            await self.app(scope, receive, send)
            return
        if self._cookie_ok(scope) or self.config.token_ok(_bearer(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("method") == "GET" and "text/html" in (_header(scope, b"accept") or ""):
            await _signed_out(send)  # a browser tab: explain, don't dump JSON
            return
        await _respond(send, 401, "unauthenticated", "authentication required")

    def _cookie_ok(self, scope: Scope) -> bool:
        """A session id we issued; the local token itself still counts (older cookies)."""
        sessions = self.config.sessions
        return any(
            sessions.session_ok(v) or self.config.token_ok(v)
            for v in _cookies(scope, self.config.cookie_name)
        )

    def _secure_cookie(self, scope: Scope) -> bool:
        """``Secure`` over https and for proxy names (SEC-46), never on plain loopback,
        where some browsers would drop it."""
        host = (_header(scope, b"host") or "").lower()
        extra = {h.lower() for h in self.config.extra_hosts}
        return scope.get("scheme") in ("https", "wss") or host in extra

    async def _exchange(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (query.get("token") or [None])[0]
        headers: list[tuple[bytes, bytes]] = [(b"location", b"/"), (b"cache-control", b"no-store")]
        if not self.config.no_auth:
            # SEC-43: a sign-in code works once. The local token is still accepted so the
            # token file keeps working as a fallback, but it never goes into a cookie.
            if not (self.config.sessions.redeem(token) or self.config.token_ok(token)):
                await _respond(send, 403, "invalid_token", "invalid or expired token")
                return
            cookie = (
                f"{self.config.cookie_name}={self.config.sessions.new_session()}; "
                "HttpOnly; SameSite=Strict; Path=/"
            )
            if self._secure_cookie(scope):
                cookie += "; Secure"
            headers.append((b"set-cookie", cookie.encode()))
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def _sign_in_link(self, scope: Scope, send: Send) -> None:
        """``POST /auth/sign-in-link`` with the local token as Bearer: a new one-time
        sign-in code for ``rifferr sign-in`` (SEC-43). A browser session can't mint one."""
        if scope.get("method", "GET").upper() != "POST":
            await _respond(send, 405, "method_not_allowed", "use POST")
            return
        if self.config.no_auth:
            await _respond(send, 409, "no_auth", "the server runs without auth")
            return
        if not self.config.token_ok(_bearer(scope)):
            await _respond(send, 401, "unauthenticated", "authentication required")
            return
        sessions = self.config.sessions
        body = json.dumps(
            {
                "path": f"{EXCHANGE_PATH}?token={sessions.sign_in_code()}",
                "expires_in": sessions.code_ttl,
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def secure(app: ASGIApp, config: SecurityConfig) -> ASGIApp:
    """Wrap ``app`` in the full F-SECURITY stack."""
    inner: ASGIApp = TokenAuth(app, config)
    inner = JsonOnly(inner, config)
    inner = OriginCheck(inner, config)
    inner = HostAllowlist(inner, config)
    return SecurityHeaders(inner)
