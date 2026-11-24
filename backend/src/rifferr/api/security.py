"""Request security for the HTTP server (F-SECURITY), as plain ASGI middleware.

No framework dependency: F-API mounts ``secure(app, config)`` around its app.
Order, outermost first: security headers, Host allowlist, Origin / fetch-site,
JSON-only, token auth (SECURITY-2..7, -13). Everything except auth stays on
with ``--no-auth``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Iterable, MutableMapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import parse_qs

if TYPE_CHECKING:
    from rifferr.passwords import PasswordCheck

log = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

EXCHANGE_PATH = "/auth/exchange"
SIGN_IN_LINK_PATH = "/auth/sign-in-link"
SIGN_IN_PATH = "/auth/sign-in"
SIGN_OUT_PATH = "/auth/sign-out"
SIGN_OUT_ALL_PATH = "/auth/sign-out-all"
STATUS_PATH = "/auth/status"
SIGN_IN_SCRIPT_PATH = "/auth/sign-in.js"
MAX_SIGN_IN_BODY = 4096
DAY = 24 * 3600.0
TOUCH_EVERY = 3600.0
"""A session's use is recorded at most this often (SECURITY-15)."""
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


StartedBy = Literal["code", "password"]


@dataclass(frozen=True)
class SessionRecord:
    """One browser session (SECURITY-15). Times are Unix seconds."""

    created_at: float
    last_used_at: float
    expires_at: float
    started_by: str
    label: str = ""


class SessionStore(Protocol):
    """Where sessions live, keyed by the SHA-256 (hex) of the session id."""

    def add(self, key: str, record: SessionRecord) -> None: ...
    def get(self, key: str) -> SessionRecord | None: ...
    def touch(self, key: str, last_used_at: float, expires_at: float) -> None: ...
    def delete(self, key: str) -> None: ...
    def delete_all(self) -> None: ...
    def purge(self, now: float) -> None: ...


class MemorySessionStore:
    """Sessions in memory: tests, and servers started without an app DB."""

    def __init__(self, cap: int = 256) -> None:
        self.cap = cap
        self._rows: OrderedDict[str, SessionRecord] = OrderedDict()

    def add(self, key: str, record: SessionRecord) -> None:
        self._rows[key] = record
        while len(self._rows) > self.cap:
            self._rows.popitem(last=False)  # drop the oldest

    def get(self, key: str) -> SessionRecord | None:
        return self._rows.get(key)

    def touch(self, key: str, last_used_at: float, expires_at: float) -> None:
        row = self._rows.get(key)
        if row is not None:
            self._rows[key] = replace(row, last_used_at=last_used_at, expires_at=expires_at)

    def delete(self, key: str) -> None:
        self._rows.pop(key, None)

    def delete_all(self) -> None:
        self._rows.clear()

    def purge(self, now: float) -> None:
        for key in [k for k, r in self._rows.items() if r.expires_at <= now]:
            del self._rows[key]


def user_agent_label(value: str | None) -> str:
    """A short label for a session list, from the User-Agent."""
    text = (value or "").strip()
    browser = next(
        (b for b in ("Firefox", "Edg", "Chrome", "Safari") if b in text), text[:40] or "unknown"
    )
    browser = {"Edg": "Edge"}.get(browser, browser)
    system = next((o for o in ("Windows", "Android", "iPhone", "Mac", "Linux") if o in text), "")
    return f"{browser} on {system}" if system else browser


class Sessions:
    """Browser sessions (SECURITY-15) and one-time sign-in codes (SEC-42, -43).

    The cookie holds a session id, never the local token, so a leaked cookie is not a
    bearer token. Sessions live in ``store``: the app DB on a server, so they survive a
    restart. Sign-in codes stay in memory and work once, for ``code_ttl`` seconds. Only
    SHA-256 digests are kept, so lookups don't compare secrets byte by byte.
    """

    def __init__(
        self,
        *,
        code_ttl: float = 15 * 60,
        max_sessions: int = 256,
        max_codes: int = 32,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        store: SessionStore | None = None,
        idle: float = 30 * DAY,
        max_age: float = 90 * DAY,
    ) -> None:
        self.code_ttl = code_ttl
        self.max_codes = max_codes
        self.clock = clock
        self.wall = wall
        self.store: SessionStore = store or MemorySessionStore(max_sessions)
        self.idle = idle
        self.max_age = max_age
        self._codes: OrderedDict[bytes, float] = OrderedDict()
        self._purged_at = 0.0

    @staticmethod
    def _digest(value: str) -> bytes:
        return hashlib.sha256(value.encode()).digest()

    @staticmethod
    def key(sid: str) -> str:
        return hashlib.sha256(sid.encode()).hexdigest()

    def sign_in_code(self) -> str:
        code = secrets.token_urlsafe(TOKEN_BYTES)
        self._codes[self._digest(code)] = self.clock() + self.code_ttl
        while len(self._codes) > self.max_codes:
            self._codes.popitem(last=False)
        return code

    def redeem(self, code: str | None) -> bool:
        """True once for a live code; the code is gone afterwards."""
        if not code:
            return False
        expires = self._codes.pop(self._digest(code), None)
        return expires is not None and self.clock() < expires

    def _purge(self, now: float) -> None:
        if now - self._purged_at >= TOUCH_EVERY:
            self._purged_at = now
            self.store.purge(now)

    def new_session(self, started_by: StartedBy = "code", label: str = "") -> str:
        now = self.wall()
        self._purge(now)
        sid = secrets.token_urlsafe(TOKEN_BYTES)
        end = now + min(self.idle, self.max_age)
        self.store.add(self.key(sid), SessionRecord(now, now, end, started_by, label))
        return sid

    def check(self, sid: str | None) -> tuple[SessionRecord | None, bool]:
        """The live session ``sid`` names, and whether its use was just recorded (at most
        hourly, which moves its end); None when it's unknown or has ended."""
        if not sid:
            return None, False
        now = self.wall()
        self._purge(now)
        key = self.key(sid)
        row = self.store.get(key)
        if row is None:
            return None, False
        if now >= row.expires_at:
            self.store.delete(key)
            return None, False
        if now - row.last_used_at >= TOUCH_EVERY:
            end = min(now + self.idle, row.created_at + self.max_age)
            self.store.touch(key, now, end)
            return replace(row, last_used_at=now, expires_at=end), True
        return row, False

    def lookup(self, sid: str | None) -> SessionRecord | None:
        return self.check(sid)[0]

    def session_ok(self, sid: str | None) -> bool:
        return self.lookup(sid) is not None

    def max_age_of(self, row: SessionRecord) -> int:
        return max(0, round(row.expires_at - self.wall()))

    def end(self, sid: str | None) -> None:
        if sid:
            self.store.delete(self.key(sid))

    def end_all(self) -> None:
        self.store.delete_all()


class SignInThrottle:
    """SECURITY-19: password failures per client address and in all, in memory."""

    def __init__(
        self,
        *,
        per_address: int = 5,
        total: int = 20,
        window: float = 15 * 60,
        pause: float = 15 * 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_address = per_address
        self.total = total
        self.window = window
        self.pause = pause
        self.clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()
        self._blocked: dict[str, float] = {}
        self._paused_until = 0.0

    def _trim(self, times: deque[float], now: float) -> None:
        while times and times[0] <= now - self.window:
            times.popleft()

    def retry_after(self, address: str) -> float | None:
        """Seconds until ``address`` may try again, or None when it may now."""
        now = self.clock()
        until = max(self._blocked.get(address, 0.0), self._paused_until)
        return until - now if until > now else None

    def failed(self, address: str) -> None:
        now = self.clock()
        times = self._failures.setdefault(address, deque())
        times.append(now)
        self._trim(times, now)
        self._all.append(now)
        self._trim(self._all, now)
        if len(times) >= self.per_address:
            self._blocked[address] = now + self.pause
            times.clear()
        if len(self._all) >= self.total:
            self._paused_until = now + self.pause
            self._all.clear()
        for addr in [a for a, t in self._blocked.items() if t <= now]:
            del self._blocked[addr]


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
    passwords: PasswordCheck | None = field(default=None, compare=False)
    """The password login (SECURITY-17, -18); None: no password sign-in."""
    throttle: SignInThrottle = field(default_factory=SignInThrottle, compare=False)

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
    """SECURITY-1, -9, -20: beyond loopback only with a password login (``real_auth``)."""
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


def _client_address(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])  # pyright: ignore[reportUnknownArgumentType]
    return "unknown"


def password_transport_ok(scope: Scope, config: SecurityConfig) -> bool:
    """SECURITY-20: loopback to a loopback name, served over TLS, or through the https
    origin of a configured extra host (a reverse proxy)."""
    if scope.get("scheme") in ("https", "wss"):
        return True
    host = (_header(scope, b"host") or "").lower()
    extra = {h.lower() for h in config.extra_hosts}
    origin = (_header(scope, b"origin") or "").lower()
    if host in extra:
        return origin == f"https://{host}"
    loopback_names = {
        f"127.0.0.1:{config.port}",
        f"localhost:{config.port}",
        f"[::1]:{config.port}",
    }
    return host in loopback_names and is_loopback(_client_address(scope))


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    body = b""
    while True:
        message = await receive()
        if message.get("type") != "http.request":
            return None
        body += message.get("body", b"")
        if len(body) > limit:
            return None
        if not message.get("more_body", False):
            return body


async def _send_json(
    send: Send, status: int, data: Any, headers: Iterable[tuple[bytes, bytes]] = ()
) -> None:
    body = json.dumps(data).encode()
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


async def _no_content(send: Send, headers: Iterable[tuple[bytes, bytes]] = ()) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 204,
            "headers": [(b"cache-control", b"no-store"), *headers],
        }
    )
    await send({"type": "http.response.body", "body": b""})


SIGN_IN_SCRIPT = b"""// Rifferr password sign-in (F-SECURITY SECURITY-18).
// Served without auth: no app data.
"use strict";
const form = document.getElementById("sign-in");
const error = document.getElementById("sign-in-error");
const MESSAGES = {
  bad_credentials: "Wrong user name or password.",
  https_required:
    "Password sign-in needs https. Open Rifferr at its https address, " +
    "or use a sign-in link (rifferr sign-in).",
  no_password: "No password is set. Run rifferr passwd on the server, or use a sign-in link.",
};
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  error.textContent = "";
  const data = new FormData(form);
  const r = await fetch("/auth/sign-in", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
  });
  if (r.status === 204) {
    window.location.reload();
    return;
  }
  let code = "";
  try {
    code = (await r.json()).error.code;
  } catch {}
  if (code === "too_many_attempts") {
    const minutes = Math.max(1, Math.ceil(Number(r.headers.get("retry-after") || 60) / 60));
    error.textContent = `Too many failed attempts. Try again in ${minutes} minutes.`;
  } else {
    error.textContent = MESSAGES[code] || `Sign-in failed (${r.status}).`;
  }
});
"""

SIGN_IN_FORM = b"""<h2>Sign in</h2>
<form id="sign-in" style="display: grid; gap: .5rem; max-width: 20rem">
<label>User name <input name="username" autocomplete="username" value="admin" required></label>
<label>Password <input name="password" type="password" autocomplete="current-password"
 required autofocus></label>
<button type="submit">Sign in</button>
<p id="sign-in-error" role="alert" style="color: #c92a2a"></p>
</form>
<script src="/auth/sign-in.js"></script>
<h2>Or use a sign-in link</h2>
"""


class TokenAuth:
    """SECURITY-6, -7, -15 to -21: session cookie or local token as bearer; the sign-in
    routes."""

    def __init__(self, app: ASGIApp, config: SecurityConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if scope["type"] == "http":
            route = {
                EXCHANGE_PATH: self._exchange,
                SIGN_IN_LINK_PATH: self._sign_in_link,
                SIGN_IN_PATH: self._sign_in,
                SIGN_OUT_PATH: self._sign_out,
                SIGN_OUT_ALL_PATH: self._sign_out_all,
                STATUS_PATH: self._status,
                SIGN_IN_SCRIPT_PATH: self._script,
            }.get(path)
            if route is not None:
                await route(scope, receive, send)
                return
        if (
            self.config.no_auth
            or path in self.config.public_paths
            or any(path.startswith(p) for p in self.config.own_auth_prefixes)
        ):
            await self.app(scope, receive, send)
            return
        found = self._session(scope)
        if found is not None:
            sid, row, touched = found
            if touched and scope["type"] == "http":
                await self.app(scope, receive, self._with_cookie(scope, send, sid, row))
            else:
                await self.app(scope, receive, send)
            return
        if self._token_cookie(scope) or self.config.token_ok(_bearer(scope)):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope.get("method") == "GET" and "text/html" in (_header(scope, b"accept") or ""):
            await self._signed_out(send)  # a browser tab: explain, don't dump JSON
            return
        await _respond(send, 401, "unauthenticated", "authentication required")

    # -- sessions --

    def _session(self, scope: Scope) -> tuple[str, SessionRecord, bool] | None:
        for value in _cookies(scope, self.config.cookie_name):
            row, touched = self.config.sessions.check(value)
            if row is not None:
                return value, row, touched
        return None

    def _token_cookie(self, scope: Scope) -> bool:
        """The local token itself in the cookie still counts (older cookies)."""
        return any(self.config.token_ok(v) for v in _cookies(scope, self.config.cookie_name))

    def _signed_in(self, scope: Scope) -> bool:
        return (
            self.config.no_auth
            or self._session(scope) is not None
            or self._token_cookie(scope)
            or self.config.token_ok(_bearer(scope))
        )

    def _secure_cookie(self, scope: Scope) -> bool:
        """``Secure`` over https and for proxy names (SEC-46), never on plain loopback,
        where some browsers would drop it."""
        host = (_header(scope, b"host") or "").lower()
        extra = {h.lower() for h in self.config.extra_hosts}
        return scope.get("scheme") in ("https", "wss") or host in extra

    def _cookie(self, scope: Scope, value: str, max_age: int) -> tuple[bytes, bytes]:
        cookie = (
            f"{self.config.cookie_name}={value}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={max_age}"
        )
        if self._secure_cookie(scope):
            cookie += "; Secure"
        return (b"set-cookie", cookie.encode())

    def _new_session_cookie(self, scope: Scope, started_by: StartedBy) -> tuple[bytes, bytes]:
        sessions = self.config.sessions
        sid = sessions.new_session(started_by, user_agent_label(_header(scope, b"user-agent")))
        row = sessions.lookup(sid)
        return self._cookie(scope, sid, sessions.max_age_of(row) if row else 0)

    def _with_cookie(self, scope: Scope, send: Send, sid: str, row: SessionRecord) -> Send:
        """SECURITY-15: the cookie's Max-Age follows the session's moved end."""
        header = self._cookie(scope, sid, self.config.sessions.max_age_of(row))

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), header]
            await send(message)

        return wrapped

    async def _signed_out(self, send: Send) -> None:
        """SECURITY-7: the same 401, as a page with no app data; with a password login,
        it holds the sign-in form (SECURITY-18)."""
        page = SIGNED_OUT_PAGE
        if self.config.passwords is not None and self.config.passwords.enabled():
            page = page.replace(b"<h1>Signed out</h1>\n", b"<h1>Signed out</h1>\n" + SIGN_IN_FORM)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(page)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": page})

    # -- routes --

    async def _exchange(self, scope: Scope, receive: Receive, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        token = (query.get("token") or [None])[0]
        headers: list[tuple[bytes, bytes]] = [(b"location", b"/"), (b"cache-control", b"no-store")]
        if not self.config.no_auth:
            # SEC-43: a sign-in code works once. The local token is still accepted so the
            # token file keeps working as a fallback, but it never goes into a cookie.
            if not (self.config.sessions.redeem(token) or self.config.token_ok(token)):
                await _respond(send, 403, "invalid_token", "invalid or expired token")
                return
            headers.append(self._new_session_cookie(scope, "code"))
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b""})

    async def _sign_in_link(self, scope: Scope, receive: Receive, send: Send) -> None:
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
        await _send_json(
            send,
            200,
            {
                "path": f"{EXCHANGE_PATH}?token={sessions.sign_in_code()}",
                "expires_in": sessions.code_ttl,
            },
        )

    async def _sign_in(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``POST /auth/sign-in {username, password}`` (SECURITY-18 to -20)."""
        if scope.get("method", "GET").upper() != "POST":
            await _respond(send, 405, "method_not_allowed", "use POST")
            return
        if self.config.no_auth:
            await _respond(send, 409, "no_auth", "the server runs without auth")
            return
        passwords = self.config.passwords
        if passwords is None or not passwords.enabled():
            await _respond(send, 404, "no_password", "no password is set; run rifferr passwd")
            return
        if not password_transport_ok(scope, self.config):
            await _respond(send, 403, "https_required", "password sign-in needs https")
            return
        address = _client_address(scope)
        wait = self.config.throttle.retry_after(address)
        if wait is not None:
            await _respond(
                send,
                429,
                "too_many_attempts",
                "too many failed sign-ins; try again later",
                [(b"retry-after", str(int(wait) + 1).encode())],
            )
            return
        raw = await _read_body(receive, MAX_SIGN_IN_BODY)
        try:
            data = json.loads(raw) if raw is not None else None
        except ValueError:
            data = None
        user = data.get("username") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        password = data.get("password") if isinstance(data, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(user, str) or not isinstance(password, str):
            await _respond(send, 400, "bad_request", "send a username and a password")
            return
        if not await asyncio.to_thread(passwords.verify, user, password):
            self.config.throttle.failed(address)
            log.warning("password sign-in failed from %s", address)
            await _respond(send, 401, "bad_credentials", "wrong user name or password")
            return
        log.info("password sign-in from %s", address)
        await _no_content(send, [self._new_session_cookie(scope, "password")])

    async def _sign_out(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``POST /auth/sign-out``: this browser's session ends (SECURITY-16)."""
        if scope.get("method", "GET").upper() != "POST":
            await _respond(send, 405, "method_not_allowed", "use POST")
            return
        for value in _cookies(scope, self.config.cookie_name):
            self.config.sessions.end(value)
        await _no_content(send, [self._cookie(scope, "", 0)])

    async def _sign_out_all(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``POST /auth/sign-out-all``: every session ends (SECURITY-16)."""
        if scope.get("method", "GET").upper() != "POST":
            await _respond(send, 405, "method_not_allowed", "use POST")
            return
        if self.config.no_auth or not self._signed_in(scope):
            await _respond(send, 401, "unauthenticated", "authentication required")
            return
        self.config.sessions.end_all()
        log.info("every session was ended")
        await _no_content(send, [self._cookie(scope, "", 0)])

    async def _status(self, scope: Scope, receive: Receive, send: Send) -> None:
        """``GET /auth/status``, no auth: what the sign-in screen needs to know."""
        passwords = self.config.passwords
        await _send_json(
            send,
            200,
            {
                "signed_in": self._signed_in(scope),
                "password_login": (
                    not self.config.no_auth and passwords is not None and passwords.enabled()
                ),
            },
        )

    async def _script(self, scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/javascript; charset=utf-8"),
                    (b"content-length", str(len(SIGN_IN_SCRIPT)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": SIGN_IN_SCRIPT})


def secure(app: ASGIApp, config: SecurityConfig) -> ASGIApp:
    """Wrap ``app`` in the full F-SECURITY stack."""
    inner: ASGIApp = TokenAuth(app, config)
    inner = JsonOnly(inner, config)
    inner = OriginCheck(inner, config)
    inner = HostAllowlist(inner, config)
    return SecurityHeaders(inner)
