"""Settings page routes (F-SETTINGS SETTINGS-1 to -8).

Every route is under ``/api``, so the security middleware's JSON-only, Origin, Host and
session checks apply (SETTINGS-8). Keys are never echoed or logged (SETTINGS-6).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import APIRouter
from pydantic import Field, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.ai.runner import AITaskError
from rifferr.ai.tasks.ping import PingInput, PingTask
from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import _Model  # pyright: ignore[reportPrivateUsage]
from rifferr.api.routes.ai import ai_service
from rifferr.api.routes.workflows import get_profile, workflow_files
from rifferr.api.state import ServerState, State
from rifferr.core.ai import SuggestMode, is_private_host
from rifferr.core.profile import Profile, ProfileLoadError
from rifferr.core.workflow import WorkflowLoadError, load_workflow
from rifferr.credentials import SecretNotAvailableError, SecretResolver
from rifferr.profile_store import ProfileChanged, ProfileError, profile_file_hash
from rifferr.settings import (
    KEYRING_SERVICE,
    SECTIONS,
    SettingsSection,
    key_plan,
    link_key,
    save_section,
    unlink_key,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/profiles/{profile}/settings", tags=["settings"])
MASK = "••••"


class SectionInfo(_Model):
    id: str
    title: str


class ProviderStatus(_Model):
    """What the AI section shows about a provider besides its settings (SETTINGS-5, -6)."""

    name: str
    key_set: bool = Field(description="Whether its api_key resolves; never the key")
    key_secret: str | None = Field(description="The secret name its api_key uses")
    host: str | None
    local: bool = Field(description="False: album data leaves this machine (AI-CORE-20)")


class AISuggestStep(_Model):
    """A workflow step with ``with.ai`` that a suggestion-mode override can name."""

    ref: str = Field(description='"<workflow>/<step id>"')
    workflow: str
    step_id: str
    mode: SuggestMode | None = Field(description="The step's own with.ai.mode")


class AISectionInfo(_Model):
    provider_types: list[str]
    providers: list[ProviderStatus]
    steps: list[AISuggestStep]
    keyring: bool = Field(description="Whether an OS keyring is available")


class SettingsView(_Model):
    section: str
    title: str
    values: dict[str, JsonValue]
    schema_: dict[str, JsonValue] = Field(alias="schema", serialization_alias="schema")
    base_hash: str = Field(description="Send back on PUT; 409 if the file changed since")
    ai: AISectionInfo | None = None


class SettingsBody(_Model):
    values: dict[str, JsonValue]
    base_hash: str = Field(min_length=64, max_length=64)
    confirm_hosts: list[str] = Field(
        default_factory=list[str],
        max_length=64,
        description="New hosts the user confirmed album data may go to (SETTINGS-10)",
    )


class KeyBody(_Model):
    key: str = Field(min_length=1, max_length=4096)


class ClearKeyBody(_Model):
    """Empty: mutating requests are JSON (F-SECURITY)."""


class KeyResult(_Model):
    provider: str
    key_set: bool
    keyring: bool
    secret: str
    env_var: str = Field(description="Without a keyring: the variable to set")
    env_line: str = Field(description="Without a keyring: the secrets line to add")


class TestResult(_Model):
    ok: bool
    provider: str
    model: str | None = None
    error: str | None = None


def _section(section: str) -> SettingsSection:
    found = SECTIONS.get(section)
    if found is None:
        raise not_found(f"settings section '{section}'")
    return found


@router.get("", response_model=list[SectionInfo], responses=ERROR_RESPONSES)
async def list_sections(profile: str, state: State) -> list[SectionInfo]:
    """The Settings page's sections, in order (SETTINGS-1)."""
    get_profile(state, profile)
    return [SectionInfo(id=s.id, title=s.title) for s in SECTIONS.values()]


def _host(state: ServerState, setting: Any) -> str | None:
    if setting.base_url:
        return urlsplit(setting.base_url).hostname
    providers = state.ai.providers if state.ai is not None else None
    impl = (providers or {}).get(setting.type)
    return impl.default_host if impl is not None else None


def _key_set(state: ServerState, p: Profile, secret: str | None) -> bool:
    ref = p.secrets.get(secret) if secret else None
    if ref is None:
        return False
    try:
        SecretResolver(keyring_get=state.keyring.get).resolve(ref)
    except SecretNotAvailableError:
        return False
    return True


def _ai_steps(state: ServerState, profile: str) -> list[AISuggestStep]:
    out: list[AISuggestStep] = []
    for paths in workflow_files(state, profile).values():
        try:
            wf, _ = load_workflow(paths[0].read_text(encoding="utf-8"), str(paths[0]))
        except (OSError, UnicodeDecodeError, WorkflowLoadError):
            continue
        for step in wf.steps:
            block = step.with_.get("ai")
            if not isinstance(block, dict):
                continue
            mode = cast("dict[str, Any]", block).get("mode")
            out.append(
                AISuggestStep(
                    ref=f"{wf.name}/{step.id}",
                    workflow=wf.name,
                    step_id=step.id,
                    mode=mode if mode in ("off", "unsure", "always") else None,
                )
            )
    return out


def _ai_info(state: ServerState, p: Profile) -> AISectionInfo:
    providers = state.ai.providers if state.ai is not None else None
    statuses: list[ProviderStatus] = []
    for setting in p.ai.providers:
        host = _host(state, setting)
        statuses.append(
            ProviderStatus(
                name=setting.name,
                key_set=_key_set(state, p, setting.api_key),
                key_secret=setting.api_key,
                host=host,
                local=host is not None and is_private_host(host),
            )
        )
    return AISectionInfo(
        provider_types=sorted((providers or {}).keys()),
        providers=statuses,
        steps=_ai_steps(state, p.name),
        keyring=state.keyring.available(),
    )


def _view(state: ServerState, profile: str, section: SettingsSection) -> SettingsView:
    p = get_profile(state, profile)
    value = section.read(p)
    return SettingsView(
        section=section.id,
        title=section.title,
        values=value.model_dump(mode="json"),
        schema=section.model.model_json_schema(),  # pyright: ignore[reportCallIssue]
        base_hash=profile_file_hash(state.config_root, p.name),
        ai=_ai_info(state, p) if section.id == "ai" else None,
    )


@router.get("/{section}", response_model=SettingsView, responses=ERROR_RESPONSES)
async def get_section(profile: str, section: str, state: State) -> SettingsView:
    """The section's values, their JSON Schema and the file's hash (SETTINGS-2)."""
    return _view(state, profile, _section(section))


def _remote_hosts(state: ServerState, providers: Any) -> dict[str, list[str]]:
    """Non-local hosts of the enabled providers, each with the providers that use it."""
    out: dict[str, list[str]] = {}
    for setting in providers:
        host = _host(state, setting) if setting.enabled else None
        if host is not None and not is_private_host(host):
            out.setdefault(host, []).append(setting.name)
    return out


def _check_new_hosts(state: ServerState, p: Profile, value: Any, confirmed: list[str]) -> None:
    """Refuse a save that starts sending album data to an unconfirmed host (SETTINGS-10)."""
    known = _remote_hosts(state, p.ai.providers)
    new = {
        h: names
        for h, names in _remote_hosts(state, value.providers).items()
        if h not in known and h not in confirmed
    }
    if new:
        details: list[JsonValue] = [
            {"host": h, "providers": list(names)} for h, names in sorted(new.items())
        ]
        raise ApiError(
            409,
            "confirm_hosts",
            f"album data would go to a new host: {', '.join(sorted(new))}; confirm to save",
            details=details,
        )


def _field_errors(exc: PydanticValidationError) -> list[JsonValue]:
    return [
        {"field": ".".join(str(x) for x in e["loc"]), "message": e["msg"]}
        for e in exc.errors(include_url=False)
    ]


@router.put("/{section}", response_model=SettingsView, responses=ERROR_RESPONSES)
async def put_section(profile: str, section: str, body: SettingsBody, state: State) -> SettingsView:
    """Validate the whole section and write only its key (SETTINGS-2, -3). ``400`` lists the
    fields at fault; ``409 conflict`` when ``profile.yaml`` changed since ``base_hash``, and
    ``409 confirm_hosts`` when album data would go to a host not yet confirmed (SETTINGS-10)."""
    found = _section(section)
    p = get_profile(state, profile)
    try:
        value = found.model.model_validate(body.values)
    except PydanticValidationError as exc:
        errors = _field_errors(exc)
        where = ", ".join(".".join(str(x) for x in e["loc"]) for e in exc.errors())
        raise ApiError(400, "invalid_settings", f"invalid: {where}", details=errors) from None
    if found.id == "ai":
        _check_new_hosts(state, p, value, body.confirm_hosts)
    try:
        await asyncio.to_thread(
            save_section, state.config_root, p.name, found, value, body.base_hash
        )
    except ProfileChanged:
        raise ApiError(
            409, "conflict", "profile.yaml changed since it was loaded; load it again"
        ) from None
    except ProfileLoadError as exc:
        details: list[JsonValue] = [{"field": e.path, "message": e.message} for e in exc.errors]
        raise ApiError(
            400, "invalid_settings", f"the profile would be invalid: {exc}", details=details
        ) from None
    except ProfileError as exc:
        raise not_found(str(exc)) from None
    log.info("profile %s: settings section %s saved", p.name, found.id)
    return _view(state, profile, found)


# -- provider keys (SETTINGS-6) -------------------------------------------------------


def _provider(p: Profile, name: str) -> None:
    if all(x.name != name for x in p.ai.providers):
        raise not_found(f"AI provider '{name}' in profile '{p.name}'")


def _key_result(state: ServerState, profile: str, provider: str) -> KeyResult:
    p = get_profile(state, profile)
    plan = key_plan(profile, provider)
    setting = next(x for x in p.ai.providers if x.name == provider)
    return KeyResult(
        provider=provider,
        key_set=_key_set(state, p, setting.api_key),
        keyring=state.keyring.available(),
        secret=plan.secret,
        env_var=plan.env_var,
        env_line=plan.env_line,
    )


@router.put("/ai/providers/{provider}/key", response_model=KeyResult, responses=ERROR_RESPONSES)
async def set_key(profile: str, provider: str, body: KeyBody, state: State) -> KeyResult:
    """Store the key in the OS keyring and point the provider at it. The key is never
    returned, logged or written to ``profile.yaml``. Without a keyring: ``409 no_keyring``
    with the environment variable to use instead."""
    p = get_profile(state, profile)
    _provider(p, provider)
    plan = key_plan(profile, provider)
    if not state.keyring.available():
        raise ApiError(
            409,
            "no_keyring",
            f"No OS keyring here. Set the environment variable {plan.env_var} to the key "
            f"and add this to profile.yaml: {plan.env_line}; then set the provider's "
            f"api_key to {plan.secret}.",
            details={"env_var": plan.env_var, "env_line": plan.env_line, "secret": plan.secret},
        )
    await asyncio.to_thread(state.keyring.set, KEYRING_SERVICE, plan.keyring_name, body.key)
    try:
        await asyncio.to_thread(link_key, state.config_root, p.name, provider, plan)
    except ProfileLoadError as exc:
        await asyncio.to_thread(state.keyring.delete, KEYRING_SERVICE, plan.keyring_name)
        raise ApiError(400, "invalid_settings", f"the profile would be invalid: {exc}") from None
    log.info("profile %s: stored the key for AI provider %s in the keyring", p.name, provider)
    return _key_result(state, profile, provider)


@router.delete("/ai/providers/{provider}/key", response_model=KeyResult, responses=ERROR_RESPONSES)
async def clear_key(profile: str, provider: str, state: State, body: ClearKeyBody) -> KeyResult:
    """Remove the key from the keyring and the provider's ``api_key`` (SETTINGS-6)."""
    p = get_profile(state, profile)
    _provider(p, provider)
    plan = key_plan(profile, provider)
    if state.keyring.available():
        await asyncio.to_thread(state.keyring.delete, KEYRING_SERVICE, plan.keyring_name)
    setting = next(x for x in p.ai.providers if x.name == provider)
    if setting.api_key == plan.secret:
        await asyncio.to_thread(unlink_key, state.config_root, p.name, provider, plan.secret)
    log.info("profile %s: cleared the key for AI provider %s", p.name, provider)
    return _key_result(state, profile, provider)


# -- test connection (SETTINGS-7) -----------------------------------------------------


def _masked(state: ServerState, p: Profile, secret: str | None, text: str) -> str:
    ref = p.secrets.get(secret) if secret else None
    if ref is None:
        return text
    try:
        value = SecretResolver(keyring_get=state.keyring.get).resolve(ref).reveal()
    except SecretNotAvailableError:
        return text
    return text.replace(value, MASK) if value else text


@router.post("/ai/providers/{provider}/test", response_model=TestResult, responses=ERROR_RESPONSES)
async def test_provider(profile: str, provider: str, state: State) -> TestResult:
    """One tiny fixed request, no album data, never cached (SETTINGS-7)."""
    p = get_profile(state, profile)
    _provider(p, provider)
    setting = next(x for x in p.ai.providers if x.name == provider)
    runtime = ai_service(state).runtime(p.name)
    try:
        result = await runtime.run_task(
            PingTask(), PingInput(), provider=provider, refresh=True, retries=0
        )
    except AITaskError as exc:
        return TestResult(
            ok=False, provider=provider, error=_masked(state, p, setting.api_key, exc.message)
        )
    return TestResult(ok=bool(result.value), provider=provider, model=result.model)
