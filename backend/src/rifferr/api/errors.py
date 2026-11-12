"""The single error envelope (API-11) and its exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from rifferr.api.models import ErrorEnvelope


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: JsonValue | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def envelope(code: str, message: str, details: JsonValue | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorEnvelope} for status in (400, 404, 409, 422)
}

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed"}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(envelope(exc.code, exc.message, exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        details: list[JsonValue] = [
            {"loc": [str(p) for p in e["loc"]], "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(envelope("bad_request", "invalid request", details), status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, "bad_request")
        return JSONResponse(envelope(code, str(exc.detail)), status_code=exc.status_code)
