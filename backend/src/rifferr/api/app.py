"""The HTTP server (F-API): FastAPI app, lifespan, static frontend, security stack.

``create_api`` builds the bare FastAPI app (used for the OpenAPI document);
``create_app`` wraps it in the F-SECURITY middleware. ``rifferr serve`` (F-CLI)
runs the latter under uvicorn.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.types import Scope

from rifferr import __version__
from rifferr.api import errors
from rifferr.api.models import Health
from rifferr.api.routes import profiles, runs, scripts, workflows
from rifferr.api.security import (
    ASGIApp,
    SecurityConfig,
    generate_token,
    remove_token_file,
    secure,
    write_token_file,
)
from rifferr.api.state import ServerState
from rifferr.appdb import AppDB, SqliteEventSink
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import app_db_path, config_root
from rifferr.steps.registry import load_step_types
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


SERVER_PREFIXES = ("/api/", "/auth/", "/healthz", "/openapi.json")


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

    def security(self) -> SecurityConfig:
        dev_origins: tuple[str, ...] = ()
        if self.dev:  # API-13: the Vite origin, in --dev only
            dev_origins = (
                f"http://localhost:{self.vite_port}",
                f"http://127.0.0.1:{self.vite_port}",
            )
        return SecurityConfig(
            port=self.port, token=self.token, no_auth=self.no_auth, dev_origins=dev_origins
        )


def create_api(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = AppDB(settings.db_path or app_db_path())  # migrates (API-1)
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
        supervisor.start()  # decides overdue review gates now and as deadlines pass
        app.state.rifferr = state
        if settings.write_token and not settings.no_auth:
            write_token_file(settings.port, settings.token)
        try:
            yield
        finally:
            await previews.shutdown()
            await supervisor.shutdown()
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
    settings = settings or ApiSettings()
    return secure(create_api(settings), settings.security())
