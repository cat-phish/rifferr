"""Tag-script files and live preview (API-18, TAGSCRIPT-20, -21)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import ScriptDetail, ScriptError, ScriptSave, ScriptSummary
from rifferr.api.readonly import READ_ONLY_RESPONSES, STATUS, ask
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import State
from rifferr.profile_store import profile_dir
from rifferr.script_store import (
    ScriptHashMismatch,
    ScriptPathError,
    read_script,
    save_script,
    script_files,
)
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.preview import TIMEOUT, PreviewRequest, PreviewResponse

router = APIRouter(prefix="/api/profiles/{profile}", tags=["scripts"])


def _folder(state: State, profile: str) -> Path:
    get_profile(state, profile)  # 404 / 422 for unknown or broken profiles
    return profile_dir(state.config_root, profile)


def _path_error(exc: ScriptPathError) -> ApiError:
    return ApiError(STATUS.get(exc.code, 400), exc.code, str(exc))


def _errors(text: str, name: str) -> list[ScriptError]:
    _, errors = parse_script(text, name)
    return [
        ScriptError(path=e.path, message=e.message, line=e.line, column=e.column) for e in errors
    ]


def _detail(name: str, file: Path, text: str, sha256: str) -> ScriptDetail:
    errors = _errors(text, file.name)
    return ScriptDetail(
        name=name, file=file.name, sha256=sha256, valid=not errors, errors=errors, text=text
    )


@router.get("/scripts", response_model=list[ScriptSummary], responses=ERROR_RESPONSES)
async def list_scripts(profile: str, state: State) -> list[ScriptSummary]:
    """Script files with their hash and validity (API-18)."""
    folder = _folder(state, profile)
    out: list[ScriptSummary] = []
    for name, files in script_files(folder).items():
        if len(files) > 1:
            names = ", ".join(f.name for f in files)
            error = ScriptError(path="", message=f"ambiguous: {names}")
            out.append(ScriptSummary(name=name, file=names, sha256="", valid=False, errors=[error]))
            continue
        try:
            found = read_script(folder, name)
        except ScriptPathError as exc:
            error = ScriptError(path="", message=str(exc))
            out.append(
                ScriptSummary(name=name, file=files[0].name, sha256="", valid=False, errors=[error])
            )
            continue
        if found is not None:
            d = _detail(name, found.path, found.text, found.sha256)
            out.append(ScriptSummary.model_validate(d.model_dump(exclude={"text"})))
    return out


@router.get("/scripts/{name}", response_model=ScriptDetail, responses=ERROR_RESPONSES)
async def get_script(profile: str, name: str, state: State) -> ScriptDetail:
    folder = _folder(state, profile)
    try:
        found = read_script(folder, name)
    except ScriptPathError as exc:
        raise _path_error(exc) from None
    if found is None:
        raise not_found(f"script '{name}' in profile '{profile}'")
    return _detail(name, found.path, found.text, found.sha256)


@router.put("/scripts/{name}", response_model=ScriptDetail, responses=ERROR_RESPONSES)
async def put_script(profile: str, name: str, body: ScriptSave, state: State) -> ScriptDetail:
    """Save the text as typed; with ``sha256``, only if the file is unchanged (API-18)."""
    folder = _folder(state, profile)
    try:
        saved = save_script(folder, name, body.text, body.sha256)
    except ScriptPathError as exc:
        raise _path_error(exc) from None
    except ScriptHashMismatch:
        raise ApiError(
            409, "hash_mismatch", f"script '{name}' changed since it was loaded; reload it"
        ) from None
    return _detail(name, saved.path, saved.text, saved.sha256)


@router.post("/tagscript/preview", response_model=PreviewResponse, responses=READ_ONLY_RESPONSES)
async def preview_script(profile: str, body: PreviewRequest, state: State) -> PreviewResponse:
    """Evaluate a script against a query in the read-only worker (TAGSCRIPT-20, -21).

    Nothing is proposed, written or stored.
    """
    _folder(state, profile)
    result = await ask(
        state,
        profile,
        "tagscript.preview",
        body.model_dump(mode="json", exclude_none=True),
        timeout=TIMEOUT,
        timeout_code="preview_timeout",
        client=body.client,
    )
    return PreviewResponse.model_validate(result)
