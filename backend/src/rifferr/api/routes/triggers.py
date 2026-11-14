"""Trigger listing and webhook secrets (F-TRIGGERS TRIGGERS-4, -17)."""

from __future__ import annotations

import re

from fastapi import APIRouter

from rifferr.api.errors import ERROR_RESPONSES, ApiError
from rifferr.api.models import TriggerStateOut, WebhookSecret
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import State
from rifferr.appdb import AppTriggers
from rifferr.core.workflow.models import NAME_PATTERN
from rifferr.triggers.secrets import new_secret

router = APIRouter(prefix="/api/profiles/{profile}", tags=["triggers"])


@router.get("/triggers", response_model=list[TriggerStateOut], responses=ERROR_RESPONSES)
async def list_triggers(profile: str, state: State) -> list[TriggerStateOut]:
    get_profile(state, profile)
    if state.triggers is None:
        return []
    store = AppTriggers(state.db)
    out: list[TriggerStateOut] = []
    for info in state.triggers.list(profile):
        last = store.last_fired(profile, info.workflow, info.kind)
        has_secret = None
        if info.kind == "webhook":
            armed = state.triggers.armed.get((profile, info.workflow))
            hook = (
                armed.workflow.on.webhook
                if armed and armed.workflow and armed.workflow.on
                else None
            )
            has_secret = hook is not None and store.webhook_hash(profile, hook.name) is not None
        out.append(
            TriggerStateOut(
                workflow=info.workflow,
                kind=info.kind,
                state=info.state,
                detail=info.detail,
                path=info.path,
                next_fire=info.next_fire,
                last_fired=last.created_at if last else None,
                last_run_id=last.run if last else None,
                has_secret=has_secret,
            )
        )
    return out


@router.post(
    "/webhooks/{name}/secret",
    status_code=201,
    response_model=WebhookSecret,
    responses=ERROR_RESPONSES,
)
async def create_webhook_secret(profile: str, name: str, state: State) -> WebhookSecret:
    """Create or replace a webhook's secret; the response is the only time it is shown."""
    get_profile(state, profile)
    if not re.match(NAME_PATTERN, name):
        raise ApiError(400, "bad_request", f"invalid webhook name {name!r}")
    secret = new_secret(AppTriggers(state.db), profile, name)
    return WebhookSecret(
        profile=profile, name=name, url_path=f"/hooks/{profile}/{name}", secret=secret
    )
