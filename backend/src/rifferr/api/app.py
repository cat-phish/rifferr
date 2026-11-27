"""The HTTP server (F-API): FastAPI app, lifespan, static frontend, security stack.

``create_api`` builds the bare FastAPI app (used for the OpenAPI document);
``create_app`` wraps it in the F-SECURITY middleware. ``rifferr serve`` (F-CLI)
runs the latter under uvicorn.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from rifferr import __version__
from rifferr.ai.cache import AppAICache
from rifferr.ai.service import AIService
from rifferr.api import errors
from rifferr.api.models import Health
from rifferr.api.routes import (
    ai,
    covers,
    dashboard,
    hooks,
    library,
    profiles,
    review,
    runs,
    scripts,
    triggers,
    workflows,
)
from rifferr.api.routes import settings as settings_routes
from rifferr.api.security import (
    DAY,
    ASGIApp,
    SecurityConfig,
    SessionRecord,
    Sessions,
    generate_token,
    normalize_host,
    remove_token_file,
    secure,
    write_token_file,
)
from rifferr.api.state import ServerState, publish_profile_event
from rifferr.appdb import AppDB, SqliteEventSink
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import StepTypeRegistry
from rifferr.notify import Notifier, default_notifier
from rifferr.notify.service import NotificationService
from rifferr.passwords import PasswordCheck, auth_path
from rifferr.profile_store import app_db_path, config_root, load_profile
from rifferr.steps.registry import load_step_types
from rifferr.triggers import TriggerManager
from rifferr.worker.previews import PreviewSupervisor
from rifferr.worker.supervisor import Command, WorkerSupervisor

DEFAULT_PORT = 8765
VITE_PORT = 5173
KEEPALIVE = 15.0
REPO_FRONTEND = Path(__file__).resolve().parents[4] / "frontend" / "dist"

NO_FRONTEND = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Rifferr</title></head>
<body><h1>Rifferr</h1>
<p>The API is running, but no frontend build was found.</p>
<p>Build it with <code>pnpm build</code> in <code>frontend/</code>, or start the
dev server with <code>rifferr serve --dev</code>.</p></body></html>
"""


HOOKS_PREFIX = "/hooks/"
SERVER_PREFIXES = ("/api/", "/auth/", HOOKS_PREFIX, "/healthz", "/openapi.json")


class SpaStaticFiles(StaticFiles):
    """The built frontend; unknown page paths get ``index.html`` (API-13), so client-side
    routes like ``/runs/<id>`` survive a reload. Server paths and missing assets keep
    their 404."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            request_path: str = scope.get("path", "")
            accept = dict(scope.get("headers", [])).get(b"accept", b"")
            if (
                exc.status_code != 404
                or scope.get("method") not in ("GET", "HEAD")
                or b"text/html" not in accept  # only page navigations, not missing assets
                or request_path == "/api"
                or request_path.startswith(SERVER_PREFIXES)
            ):
                raise
            return await super().get_response("index.html", scope)


@dataclass
class ApiSettings:
    port: int = DEFAULT_PORT
    host: str = "127.0.0.1"
    no_auth: bool = False
    dev: bool = False
    vite_port: int = VITE_PORT
    frontend_dir: Path | None = REPO_FRONTEND
    config_root: Path | None = None
    db_path: Path | None = None
    token: str = field(default_factory=generate_token)
    worker_command: Command | None = None
    preview_command: Command | None = None
    """Read-only worker command (WORKER-11); default: ``python -m rifferr.worker --read-only``."""
    registry: StepTypeRegistry | None = None
    """Step types the server validates against; default: the installed entry points."""
    keepalive: float = KEEPALIVE
    write_token: bool = True
    """Write the token file for the CLI (SECURITY-8). Tests turn it off."""
    triggers: bool = True
    """Arm workflows' ``on:`` triggers (F-TRIGGERS)."""
    trigger_reconcile: float = 2.0
    """Seconds between checks for changed workflow files and trust (TRIGGERS-1)."""
    notifier: Notifier | None = None
    """Sends notifications (S-NOTIFY); default: Apprise. Tests pass a fake."""
    notify_sleep: Callable[[float], Awaitable[None]] | None = None
    """How rule notifications wait between retries; tests skip the wait."""
    extra_hosts: tuple[str, ...] = ()
    """Extra ``Host`` names to accept, e.g. a reverse proxy's (SEC-46). Default: none."""
    sessions: Sessions = field(default_factory=Sessions)
    """Browser sessions and one-time sign-in codes (SEC-42, -43)."""
    session_idle: float = 30 * DAY
    """A session ends after this many seconds unused (SECURITY-15)."""
    session_max: float = 90 * DAY
    """... and at the latest this many seconds after it started (SECURITY-15)."""
    passwords: PasswordCheck | None = None
    """The password login (SECURITY-17); default: ``auth.json`` in the config dir."""

    def __post_init__(self) -> None:
        self.extra_hosts = tuple(normalize_host(h) for h in self.extra_hosts)
        self.sessions.idle = self.session_idle
        self.sessions.max_age = self.session_max
        if self.passwords is None and not self.no_auth:
            self.passwords = PasswordCheck(auth_path(self.config_root or config_root()))

    def security(self) -> SecurityConfig:
        dev_origins: tuple[str, ...] = ()
        if self.dev:  # API-13: the Vite origin, in --dev only
            dev_origins = (
                f"http://localhost:{self.vite_port}",
                f"http://127.0.0.1:{self.vite_port}",
            )
        return SecurityConfig(
            port=self.port,
            token=self.token,
            no_auth=self.no_auth,
            extra_hosts=self.extra_hosts,
            dev_origins=dev_origins,
            sessions=self.sessions,
            passwords=self.passwords,
            # SECURITY-3: webhooks are called by other programs, not browsers, and check
            # their own secrets (TRIGGERS-17). The Host allowlist still applies.
            exempt_prefixes=(HOOKS_PREFIX,),
            own_auth_prefixes=(HOOKS_PREFIX,),
        )


class DbSessionStore:
    """Sessions in the app DB (SECURITY-15)."""

    def __init__(self, db: AppDB) -> None:
        self.db = db

    def add(self, key: str, record: SessionRecord) -> None:
        self.db.session_add(key, record)

    def get(self, key: str) -> SessionRecord | None:
        return self.db.session_get(key)

    def touch(self, key: str, last_used_at: float, expires_at: float) -> None:
        self.db.session_touch(key, last_used_at, expires_at)

    def delete(self, key: str) -> None:
        self.db.session_delete(key)

    def delete_all(self) -> None:
        self.db.sessions_delete_all()

    def purge(self, now: float) -> None:
        self.db.sessions_purge(now)


def _add_schemas(app: FastAPI, models: Mapping[str, type[BaseModel]]) -> None:
    """Put the models of routes that read their body themselves (to cap its size) into
    the OpenAPI components, so their ``$ref`` request bodies resolve (API-10)."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        doc = generate()
        found = doc.setdefault("components", {}).setdefault("schemas", {})
        for name, model in models.items():
            found.setdefault(
                name, model.model_json_schema(ref_template="#/components/schemas/{model}")
            )
        return doc

    app.openapi = openapi  # type: ignore[method-assign]


def create_api(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = AppDB(settings.db_path or app_db_path())  # migrates (API-1)
        settings.sessions.store = DbSessionStore(db)  # SECURITY-15: survive restarts
        settings.sessions.store.purge(time.time())
        supervisor = WorkerSupervisor(db, SqliteEventSink(db), command=settings.worker_command)
        previews = PreviewSupervisor(command=settings.preview_command)
        state = ServerState(
            config_root=settings.config_root or config_root(),
            db=db,
            supervisor=supervisor,
            previews=previews,
            registry=settings.registry or load_step_types(),
            keepalive=settings.keepalive,
        )
        supervisor.subscribe(state.hub.publish)
        root = state.config_root
        notifications = NotificationService(
            db,
            load_profile=lambda name: load_profile(root, name),
            notifier=settings.notifier or default_notifier(),
            publish=state.hub.publish,
            sleep=settings.notify_sleep or asyncio.sleep,
        )
        state.notifications = notifications
        state.ai = AIService(  # C-AI-CORE
            cache=AppAICache(db),
            load_profile=lambda name: load_profile(root, name),
            secrets_for=lambda p: (
                ProfileSecrets(p.secrets, SecretResolver(keyring_get=state.keyring.get)).resolve
            ),
            emit=lambda profile, type_, data: publish_profile_event(state, profile, type_, data),
        )
        supervisor.subscribe(notifications.on_event)  # S-NOTIFY rules (NOTIFY-9)
        supervisor.subscribe_problems(notifications.on_worker_problem)
        await notifications.start()  # retries what a stopped server left pending
        supervisor.start()  # decides overdue review gates now and as deadlines pass
        app.state.rifferr = state
        if settings.triggers:
            state.triggers = TriggerManager(
                config_root=state.config_root,
                db=db,
                registry=state.registry,
                submit=supervisor.submit_run,
                publish=state.hub.publish,
                reconcile_interval=settings.trigger_reconcile,
            )
            await state.triggers.start()
        if settings.write_token and not settings.no_auth:
            write_token_file(settings.port, settings.token)
        try:
            yield
        finally:
            if state.triggers is not None:  # TRIGGERS-25: before the supervisor
                await state.triggers.stop()
            await previews.shutdown()
            await supervisor.shutdown()
            await notifications.stop()
            await state.covers.close()
            if settings.write_token and not settings.no_auth:
                remove_token_file(settings.port)
            db.close()

    app = FastAPI(
        title="Rifferr",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    errors.install(app)
    app.include_router(profiles.router)
    app.include_router(workflows.router)
    app.include_router(runs.router)
    app.include_router(scripts.router)
    app.include_router(library.router)
    app.include_router(review.router)
    app.include_router(triggers.router)
    app.include_router(hooks.router)
    app.include_router(dashboard.router)
    app.include_router(covers.router)
    app.include_router(ai.router)
    app.include_router(settings_routes.router)
    _add_schemas(app, covers.RAW_BODY_SCHEMAS)

    @app.get("/healthz", response_model=Health, tags=["health"])
    async def healthz() -> Health:
        return Health()

    frontend = settings.frontend_dir
    if frontend is not None and (frontend / "index.html").is_file():
        app.mount("/", SpaStaticFiles(directory=frontend, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def no_frontend() -> str:
            return NO_FRONTEND

    return app


def create_app(settings: ApiSettings | None = None) -> ASGIApp:
    """The FastAPI app behind the full F-SECURITY stack."""
    return create_app_and_api(settings)[0]


def create_app_and_api(settings: ApiSettings | None = None) -> tuple[ASGIApp, FastAPI]:
    """``create_app``, plus the FastAPI app inside the security stack, for ``serve`` to
    reach the running server's state (CLI-6)."""
    settings = settings or ApiSettings()
    api = create_api(settings)
    return secure(api, settings.security()), api
