from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    BeetsConfigStatus,
    NotifyTestBody,
    NotifyTestResult,
    ProfileSummary,
    TrustRequest,
)
from rifferr.api.state import State
from rifferr.beets_config import inspect_config
from rifferr.core.profile import Profile, ProfileError, ProfileLoadError
from rifferr.profile_store import list_profiles, load_profile

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.get("", response_model=list[ProfileSummary])
async def get_profiles(state: State) -> list[ProfileSummary]:
    """Profiles with variable and secret *names* only (API-2, PROFILE-5)."""
    out: list[ProfileSummary] = []
    for name in list_profiles(state.config_root):
        try:
            p = load_profile(state.config_root, name)
        except (ProfileError, ProfileLoadError) as exc:
            out.append(ProfileSummary(name=name, error=str(exc)))
            continue
        out.append(
            ProfileSummary(
                name=name,
                backend=p.backend,
                vars=sorted(p.vars),
                secrets=sorted(p.secrets),
                grid_columns=p.ui.grid_columns,
            )
        )
    return out


@router.post("/{profile}/notify/test", response_model=NotifyTestResult, responses=ERROR_RESPONSES)
async def notify_test(profile: str, body: NotifyTestBody, state: State) -> NotifyTestResult:
    """Send a fixed test notification to one target (S-NOTIFY NOTIFY-14)."""
    if profile not in list_profiles(state.config_root):
        raise not_found(f"profile '{profile}'")
    try:
        p = load_profile(state.config_root, profile)
    except (ProfileError, ProfileLoadError) as exc:
        raise ApiError(422, "profile_invalid", str(exc)) from None
    if body.target not in p.notify.targets:
        known = ", ".join(sorted(p.notify.targets)) or "none"
        raise ApiError(
            422, "unknown_target", f"no notification target '{body.target}' (known: {known})"
        )
    assert state.notifications is not None
    result = await state.notifications.send_test(p, body.target)
    return NotifyTestResult(target=result.target, sent=result.ok, error=result.error)


def _beets_status(state: State, p: Profile) -> BeetsConfigStatus:
    try:
        beetsdir = p.require_beetsdir()
    except ProfileError as exc:
        raise ApiError(404, "not_found", str(exc)) from None
    report = inspect_config(beetsdir)
    trusted = not report.needs_trust or state.db.is_trusted(report.sha256, p.name)
    return BeetsConfigStatus(
        sha256=report.sha256,
        files=report.files,
        code_keys=report.code_keys,
        needs_trust=report.needs_trust,
        trusted=trusted,
    )


def _profile_or_404(state: State, name: str) -> Profile:
    if name not in list_profiles(state.config_root):
        raise not_found(f"profile '{name}'")
    try:
        return load_profile(state.config_root, name)
    except (ProfileError, ProfileLoadError) as exc:
        raise ApiError(422, "profile_invalid", str(exc)) from None


@router.get("/{profile}/beets-config", response_model=BeetsConfigStatus, responses=ERROR_RESPONSES)
async def get_beets_config(profile: str, state: State) -> BeetsConfigStatus:
    """The keys in the profile's beets config that run code, and its trust (SEC-62)."""
    return _beets_status(state, _profile_or_404(state, profile))


@router.post(
    "/{profile}/beets-config/trust", response_model=BeetsConfigStatus, responses=ERROR_RESPONSES
)
async def trust_beets_config(profile: str, body: TrustRequest, state: State) -> BeetsConfigStatus:
    """Trust exactly the beets config content the user reviewed, in this profile (SEC-62)."""
    p = _profile_or_404(state, profile)
    status = _beets_status(state, p)
    if status.sha256 != body.sha256:
        raise ApiError(
            409, "hash_mismatch", "the beets config changed since it was reviewed; review it again"
        )
    if status.needs_trust:
        state.db.trust(
            status.sha256, str(Path(p.require_beetsdir()) / "config.yaml"), "api", p.name
        )
    return _beets_status(state, p)
