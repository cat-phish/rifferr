"""Requests to a profile's read-only worker (WORKER-11), with their HTTP errors."""

from __future__ import annotations

from typing import Any

from pydantic import JsonValue

from rifferr.api.errors import ERROR_RESPONSES, ApiError
from rifferr.api.models import ErrorEnvelope
from rifferr.api.state import ServerState
from rifferr.worker.previews import (
    PreviewFailed,
    PreviewSuperseded,
    PreviewTimeout,
    PreviewUnavailable,
)

READ_ONLY_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    503: {"model": ErrorEnvelope},
    504: {"model": ErrorEnvelope},
}

#: HTTP status of the worker's error codes; anything else is a 500.
STATUS: dict[str, int] = {
    "invalid_script_name": 400,
    "ambiguous_script_name": 409,
    "script_not_found": 404,
    "bad_request": 400,
    "bad_query": 400,
}


async def ask(
    state: ServerState,
    profile: str,
    kind: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    timeout_code: str,
    client: str | None = None,
    seq: int | None = None,
) -> dict[str, JsonValue]:
    """The worker's answer, or an ``ApiError``: ``504 <timeout_code>``, ``409 superseded``
    (a newer request from ``client``, ordered by ``seq`` when given),
    ``503 preview_unavailable``."""
    request: dict[str, Any] = {"kind": kind, "payload": payload}
    try:
        return await state.previews.request(
            profile, request, timeout=timeout, client=client, seq=seq
        )
    except PreviewTimeout as exc:
        raise ApiError(504, timeout_code, str(exc)) from None
    except PreviewSuperseded as exc:
        raise ApiError(409, "superseded", str(exc)) from None
    except PreviewUnavailable as exc:
        raise ApiError(503, "preview_unavailable", str(exc)) from None
    except PreviewFailed as exc:
        raise ApiError(STATUS.get(exc.code, 500), exc.code, str(exc)) from None
