from __future__ import annotations

from fastapi import APIRouter

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import NotifyTestBody, NotifyTestResult, ProfileSummary
from rifferr.api.state import State
from rifferr.core.profile import ProfileError, ProfileLoadError
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
