"""``POST /hooks/{profile}/{name}``: webhook triggers (F-TRIGGERS TRIGGERS-15 to -18).

Called by other programs, not browsers: exempt from the Origin check and the local
token (SECURITY-3), authenticated by the webhook's own secret, behind the Host allowlist.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from rifferr.api.errors import ERROR_RESPONSES, ApiError
from rifferr.api.models import HookAccepted
from rifferr.api.state import State
from rifferr.appdb import AppTriggers
from rifferr.triggers import WebhookError
from rifferr.triggers.secrets import credential, verify

log = logging.getLogger(__name__)
router = APIRouter(tags=["hooks"])

MAX_BODY = 1024 * 1024


async def _body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > MAX_BODY:
        raise ApiError(413, "too_large", "the body is larger than 1 MiB")
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_BODY:
            raise ApiError(413, "too_large", "the body is larger than 1 MiB")
    return bytes(data)


@router.post(
    "/hooks/{profile}/{name}",
    response_model=HookAccepted,
    status_code=202,
    responses={**ERROR_RESPONSES, 200: {"model": HookAccepted, "description": "Not started"}},
)
async def webhook(
    profile: str,
    name: str,
    request: Request,
    state: State,
    authorization: Annotated[str | None, Header()] = None,
    content_type: Annotated[str | None, Header()] = None,
) -> Any:
    if request.url.query:  # never let a secret reach access logs (TRIGGERS-17)
        raise ApiError(
            400, "bad_request", "webhooks take no query string; send the secret in Authorization"
        )
    store = AppTriggers(state.db)
    if not verify(store, profile, name, credential(authorization)):
        log.warning("webhook %s/%s: authentication failed", profile, name)
        raise ApiError(401, "unauthenticated", "authentication required")
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ApiError(415, "unsupported_media_type", "only application/json is accepted")
    raw = await _body(request)
    try:
        payload = json.loads(raw or b"null")
    except (ValueError, UnicodeDecodeError):
        raise ApiError(400, "bad_request", "the body is not valid JSON") from None
    if state.triggers is None:
        raise ApiError(404, "not_found", "no such webhook")
    try:
        result = await state.triggers.handle_webhook(profile, name, payload)
    except WebhookError as exc:
        raise ApiError(exc.status, exc.code, str(exc)) from None
    if result is None:
        return JSONResponse(HookAccepted(ignored="no_input").model_dump(exclude_none=True))
    if result.skipped is not None:
        body = HookAccepted(run_id=result.run_id, skipped=result.skipped)
        return JSONResponse(body.model_dump(exclude_none=True))
    return HookAccepted(run_id=result.run_id)
