from __future__ import annotations

from fastapi import APIRouter

from rifferr.api.models import ProfileSummary
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
