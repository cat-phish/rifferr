"""Cover search, cover images and cover review decisions (S-COVERS-SEARCH
COVERS-SEARCH-19 to -22), and what the cover picker adds: album covers outside a run,
the user's own images and the profile's provider defaults (F-COVER-PICKER).

Searches run in the server, not the beets worker: they need no library. Their results
stream as ``covers.*`` events on the profile's stream. Images are only ever those of
candidates Rifferr found itself or of the art store; the server never fetches a URL a
client sends.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Header, Query, Request, Response
from pydantic import BaseModel, JsonValue, ValidationError

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    AlbumCoverBody,
    AlbumCoverQueued,
    CoverDecided,
    CoverDecisionBody,
    CoverProviderOrder,
    CoverProvidersBody,
    CoverProvidersView,
    CoverQueryAnswer,
    CoverSearchBody,
    CoverSearchStarted,
    CoverUploadBody,
    CoverUploaded,
)
from rifferr.api.readonly import READ_ONLY_RESPONSES, ask
from rifferr.api.routes.library import queue_manual
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import ServerState, State
from rifferr.appdb import AppCoverReviews, CoverBusyError, CoverResolvedError
from rifferr.core.changeset import FieldChange, TargetRef
from rifferr.core.covers import (
    ART_FIELD,
    SHA256,
    CoverCandidate,
    CoverReview,
    mime_of,
    parse_size,
    score_candidate,
    sniff_image,
)
from rifferr.core.ids import new_ulid
from rifferr.core.profile import ProfileError, ProfileLoadError
from rifferr.covers.search import (
    UPLOAD_PROVIDER,
    DownloadError,
    ProviderStatus,
    accept_upload,
    download,
    plan,
    search,
)
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine.events import profile_stream
from rifferr.engine.manual import ManualChange, ManualChanges
from rifferr.profile_store import ProfileChanged, profile_file_hash, save_cover_providers

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["covers"])

DECIDER: Final = "ui"
ART_TIMEOUT: Final = 30.0
ART_FILE_FACTOR: Final = 4
"""An album's art file may be this many times ``covers.max_bytes``."""
IMAGE_HEADERS: Final = {
    "Content-Security-Policy": "default-src 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=3600",
}

_BINARY: Final = {"schema": {"type": "string", "format": "binary"}}
IMAGE_RESPONSE: Final[dict[str, Any]] = {
    "description": "A JPEG or PNG image",
    "content": {"image/jpeg": _BINARY, "image/png": _BINARY},
}


def _emit(state: ServerState, profile: str, type_: str, data: dict[str, JsonValue]) -> None:
    try:
        event = state.db.append_next(profile_stream(profile), type_, data)
    except Exception:
        log.exception("could not store %s", type_)
        return
    state.hub.publish(event)


async def _album_query(state: ServerState, profile: str, album_id: str) -> CoverQueryAnswer:
    """The album's cover query with its folder, from the read-only worker."""
    result = await ask(
        state,
        profile,
        "albums.cover_query",
        {"album_id": album_id},
        timeout=ART_TIMEOUT,
        timeout_code="query_timeout",
    )
    return CoverQueryAnswer.model_validate(result)


def _in_folder(candidate: CoverCandidate, folder: str | None) -> bool:
    """A local candidate's image must be in the album's folder (COVERS-SEARCH-21, -14)."""
    if candidate.path is None:
        return True
    return folder is not None and Path(candidate.path).parent == Path(folder)


def _review(state: ServerState, review_id: str) -> CoverReview:
    review = AppCoverReviews(state.db).get(review_id)
    if review is None:
        raise not_found(f"cover review {review_id}")
    return review


# -- search (COVERS-SEARCH-19) -----------------------------------------------------------


@router.post(
    "/covers/search",
    status_code=202,
    response_model=CoverSearchStarted,
    responses=ERROR_RESPONSES,
)
async def start_search(body: CoverSearchBody, state: State) -> CoverSearchStarted:
    """Ask the profile's cover providers (or ``providers``) at once; candidates, provider
    states and the ranked result arrive as ``covers.candidate``, ``covers.provider`` and
    ``covers.done`` events on the profile's stream."""
    profile = get_profile(state, body.profile)
    settings = profile.covers
    folder = None
    if body.review_id is not None and body.album_id is not None:
        raise ApiError(400, "bad_request", "give review_id or album_id, not both")
    if body.review_id is not None:
        review = _review(state, body.review_id)
        if review.profile != profile.name:
            raise not_found(f"cover review {body.review_id}")
        folder = review.query.folder
    elif body.album_id is not None:
        folder = (await _album_query(state, profile.name, body.album_id)).query.folder
    query = body.query.model_copy(update={"folder": folder})
    service = state.covers
    planned = plan(settings, body.providers)
    unknown = [n for n in planned.order if n not in service.providers]
    if unknown:
        raise ApiError(400, "bad_request", f"no cover provider named {', '.join(unknown)}")
    search_id = new_ulid()
    name = profile.name
    secrets = ProfileSecrets(profile.secrets, SecretResolver())

    def secret(key: str) -> str | None:
        try:
            return secrets.resolve(key).reveal()
        except LookupError:
            return None

    async def run() -> None:
        base: dict[str, JsonValue] = {"search_id": search_id}

        def on_candidate(c: CoverCandidate) -> None:
            scored = score_candidate(c, settings, planned.weights.get(c.provider, 1.0))
            _emit(
                state,
                name,
                "covers.candidate",
                {**base, "candidate": scored.model_dump(mode="json")},
            )

        def on_provider(s: ProviderStatus) -> None:
            _emit(state, name, "covers.provider", {**base, "status": s.model_dump(mode="json")})

        try:
            result = await search(
                query,
                settings,
                service.providers,
                service.client,
                cache=service.cache(settings),
                only=body.providers,
                refresh=body.refresh,
                secret=secret,
                on_candidate=on_candidate,
                on_provider=on_provider,
            )
        except asyncio.CancelledError:
            _emit(state, name, "covers.done", {**base, "cancelled": True})
            raise
        except Exception as exc:
            log.exception("cover search %s failed", search_id)
            _emit(state, name, "covers.done", {**base, "error": f"{type(exc).__name__}: {exc}"})
            return
        finally:
            service.searches.pop(search_id, None)
        _emit(
            state,
            name,
            "covers.done",
            {
                **base,
                "ranked": [c.model_dump(mode="json") for c in result.ranking.ranked],
                "hidden": [c.model_dump(mode="json") for c in result.ranking.hidden],
                "dropped": {k: dict(v) for k, v in result.ranking.dropped.items()},
                "statuses": [s.model_dump(mode="json") for s in result.statuses],
            },
        )

    service.searches[search_id] = asyncio.create_task(run())
    return CoverSearchStarted(search_id=search_id)


@router.delete(
    "/covers/search/{search_id}", response_model=CoverSearchStarted, responses=ERROR_RESPONSES
)
async def cancel_search(search_id: str, state: State) -> CoverSearchStarted:
    """Cancel a running search; its ``covers.done`` event says ``cancelled``."""
    task = state.covers.searches.get(search_id)
    if task is None:
        raise not_found(f"cover search {search_id}")
    task.cancel()
    return CoverSearchStarted(search_id=search_id)


# -- images (COVERS-SEARCH-20, -22) ------------------------------------------------------


def _read_capped(path: Path, limit: int) -> bytes | None:
    """The file's bytes, or None when it's larger than ``limit`` (never reads more)."""
    with path.open("rb") as f:
        data = f.read(limit + 1)
    return None if len(data) > limit else data


def _image_response(data: bytes) -> Response:
    mime = mime_of(data)
    if mime is None:
        raise ApiError(502, "bad_image", "not a JPEG or PNG image")
    return Response(content=data, media_type=mime, headers=IMAGE_HEADERS)


@router.get(
    "/covers/image/{key}",
    response_class=Response,
    responses={**ERROR_RESPONSES, 200: IMAGE_RESPONSE},
)
async def cover_image(
    key: str,
    profile: str,
    state: State,
    thumb: Annotated[bool, Query(description="The thumbnail, when the provider has one")] = False,
    review_id: Annotated[str | None, Query(description="Review a local image belongs to")] = None,
    album_id: Annotated[str | None, Query(description="Album a local image belongs to")] = None,
) -> Response:
    """An image from the profile's art store (``key`` = sha256), or a candidate's image
    or thumbnail (``key`` = candidate id): only candidates Rifferr found itself."""
    p = get_profile(state, profile)
    service = state.covers
    if SHA256.fullmatch(key):
        path = service.store(p.name).find(key)
        if path is None:
            raise not_found(f"image {key}")
        return _image_response(await asyncio.to_thread(path.read_bytes))
    candidate = service.cache(p.covers).candidate(key)
    if candidate is None:
        raise not_found(f"cover candidate {key}")
    use_thumb = thumb and candidate.thumb_url is not None
    if candidate.path is not None and not use_thumb:
        # A local image is served only for the album or review whose folder holds it.
        folder: str | None = None
        if review_id is not None:
            review = _review(state, review_id)
            if review.profile != p.name:
                raise not_found(f"cover review {review_id}")
            folder = review.query.folder
        elif album_id is not None:
            folder = (await _album_query(state, p.name, album_id)).query.folder
        if not _in_folder(candidate, folder):
            raise ApiError(400, "bad_request", "a local image must be in the album's folder")
    try:
        image = await download(
            candidate,
            p.covers,
            service.providers,
            service.client,
            cache=service.cache(p.covers),
            thumb=use_thumb,
            check=False,
        )
    except DownloadError as exc:
        raise ApiError(502, "image_unavailable", str(exc)) from None
    return _image_response(image.data)


@router.get(
    "/albums/{album_id}/art",
    response_class=Response,
    responses={**READ_ONLY_RESPONSES, 200: IMAGE_RESPONSE},
)
async def album_art(
    album_id: str,
    profile: str,
    state: State,
    if_none_match: Annotated[str | None, Header()] = None,
) -> Response:
    """The album's current art file (COVERS-SEARCH-22), read through the read-only worker.
    Its ``ETag`` is the art's sha256, so an unchanged cover answers 304 (COVER-PICKER-16)."""
    limit = parse_size(get_profile(state, profile).covers.max_bytes) * ART_FILE_FACTOR
    result = await ask(
        state,
        profile,
        "albums.art",
        {"album_id": album_id},
        timeout=ART_TIMEOUT,
        timeout_code="query_timeout",
    )
    path, sha = result.get("path"), result.get("sha256")
    if not isinstance(path, str) or not isinstance(sha, str):
        raise not_found(f"art of album {album_id}")
    etag = f'"{sha}"'
    if if_none_match is not None and etag in {t.strip() for t in if_none_match.split(",")}:
        return Response(status_code=304, headers={**IMAGE_HEADERS, "ETag": etag})
    try:
        data = await asyncio.to_thread(_read_capped, Path(path), limit)
    except OSError:
        raise not_found(f"art of album {album_id}") from None
    if data is None:
        raise ApiError(502, "bad_image", "the album's art file is too large to serve")
    if sniff_image(data[: 1 << 20]) is None and mime_of(data) is None:
        raise ApiError(502, "bad_image", "the album's art isn't a JPEG or PNG image")
    response = _image_response(data)
    response.headers["ETag"] = etag
    return response


# -- cover reviews (COVERS-SEARCH-21) ------------------------------------------------------


@router.get("/covers/reviews/{review_id}", response_model=CoverReview, responses=ERROR_RESPONSES)
async def get_cover_review(review_id: str, state: State) -> CoverReview:
    return _review(state, review_id)


@router.post(
    "/covers/reviews/{review_id}/decision",
    status_code=202,
    response_model=CoverDecided,
    responses=ERROR_RESPONSES,
)
async def decide_cover(review_id: str, body: CoverDecisionBody, state: State) -> CoverDecided:
    """``choose`` downloads the candidate (it must be a JPEG or PNG passing the size
    filters), keeps it in the art store and queues the job that sets it; ``skip`` lets
    the album go on without a cover, ``fail`` fails it."""
    review = _review(state, review_id)
    if review.state != "pending":
        raise ApiError(
            409, "already_resolved", f"cover review {review_id} is already {review.state}"
        )
    sha: str | None = None
    rationale: str | None = None
    if body.decision == "choose":
        if not body.candidate_id:
            raise ApiError(400, "bad_request", "choose needs candidate_id")
        p = get_profile(state, review.profile)
        service = state.covers
        candidate = next((c for c in review.candidates if c.id == body.candidate_id), None)
        found = candidate or service.cache(p.covers).candidate(body.candidate_id)
        if found is None:
            raise not_found(f"cover candidate {body.candidate_id}")
        if not _in_folder(found, review.query.folder):
            raise ApiError(400, "bad_request", "a local image must be in the album's folder")
        try:
            image = await download(
                found, p.covers, service.providers, service.client, cache=service.cache(p.covers)
            )
        except DownloadError as exc:
            raise ApiError(422, "bad_image", str(exc)) from None
        sha, _ = await asyncio.to_thread(service.store(p.name).put, image.data)
        size = f"{image.info.width}x{image.info.height}"
        rationale = f"cover from {found.provider} ({size}), chosen in review" + (
            f": {found.source_url}" if found.source_url else ""
        )
    try:
        job_id = await state.supervisor.decide_cover(
            review_id, body.decision, sha256=sha, rationale=rationale, by=DECIDER
        )
    except KeyError:
        raise not_found(f"cover review {review_id}") from None
    except CoverResolvedError as exc:
        raise ApiError(409, "already_resolved", str(exc)) from None
    except CoverBusyError as exc:
        raise ApiError(409, "busy", str(exc)) from None
    except ValueError as exc:
        raise ApiError(409, "not_waiting", str(exc)) from None
    return CoverDecided(job_id=job_id, sha256=sha)


# -- album covers outside a run (COVER-PICKER-2, -14) ------------------------------------


@router.get(
    "/profiles/{profile}/albums/{album_id}/cover-query",
    response_model=CoverQueryAnswer,
    responses=READ_ONLY_RESPONSES,
)
async def album_cover_query(profile: str, album_id: str, state: State) -> CoverQueryAnswer:
    """The album's cover query from the backend templates ``covers.search`` uses by default,
    its label and its art's sha256. The folder is left out."""
    get_profile(state, profile)
    answer = await _album_query(state, profile, album_id)
    return answer.model_copy(update={"query": answer.query.model_copy(update={"folder": None})})


@router.post(
    "/profiles/{profile}/albums/{album_id}/cover",
    status_code=202,
    response_model=AlbumCoverQueued,
    responses=READ_ONLY_RESPONSES,
)
async def set_album_cover(
    profile: str, album_id: str, body: AlbumCoverBody, state: State
) -> AlbumCoverQueued:
    """Download and check the candidate, keep it in the art store, and queue a manual
    ChangeSet on the album's ``art_sha256`` (undoable from Recent edits)."""
    p = get_profile(state, profile)
    service = state.covers
    found = service.cache(p.covers).candidate(body.candidate_id)
    if found is None:
        raise not_found(f"cover candidate {body.candidate_id}")
    if found.path is not None:
        folder = (await _album_query(state, p.name, album_id)).query.folder
        if not _in_folder(found, folder):
            raise ApiError(400, "bad_request", "a local image must be in the album's folder")
    try:
        image = await download(
            found, p.covers, service.providers, service.client, cache=service.cache(p.covers)
        )
    except DownloadError as exc:
        raise ApiError(422, "bad_image", str(exc)) from None
    sha, _ = await asyncio.to_thread(service.store(p.name).put, image.data)
    size = f"{image.info.width}x{image.info.height}"
    rationale = f"cover from {found.provider} ({size}), chosen in the cover picker" + (
        f": {found.source_url}" if found.source_url else ""
    )
    change = ManualChange(
        target=TargetRef(scope="album", id=album_id),
        album=album_id,
        changes=[FieldChange(field=ART_FIELD, new=sha, old=body.old_sha256)],
    )
    created = await queue_manual(
        state, p.name, ManualChanges(targets=[change], rationale=rationale)
    )
    return AlbumCoverQueued(
        changeset_id=created.changesets[0],
        job_id=created.job_id,
        sha256=sha,
        queued_behind=created.queued_behind,
    )


# -- the user's own images (COVER-PICKER-17, -18) ----------------------------------------

UPLOAD_OVERHEAD: Final = 4096
RAW_BODY_SCHEMAS: Final[dict[str, type[BaseModel]]] = {"CoverUploadBody": CoverUploadBody}


@router.post(
    "/covers/uploads",
    status_code=201,
    response_model=CoverUploaded,
    responses=ERROR_RESPONSES,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/CoverUploadBody"}}
            },
        }
    },
)
async def upload_cover(profile: str, request: Request, state: State) -> CoverUploaded:
    """Add an image of the user's own as a candidate of provider ``upload``. The body is
    JSON with the image base64-encoded; it may be at most the profile's
    ``covers.max_bytes`` once decoded."""
    p = get_profile(state, profile)
    limit = parse_size(p.covers.max_bytes)
    cap = limit * 4 // 3 + UPLOAD_OVERHEAD
    too_large = ApiError(413, "too_large", f"the image is larger than {limit} bytes")
    length = request.headers.get("content-length")
    if length is not None and length.isdigit() and int(length) > cap:
        raise too_large
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise too_large
        chunks.append(chunk)
    try:
        body = CoverUploadBody.model_validate_json(b"".join(chunks))
    except ValidationError as exc:
        raise ApiError(422, "validation_failed", str(exc.errors()[0]["msg"])) from None
    try:
        data = base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(422, "bad_image", "data isn't base64") from None
    try:
        candidate = await asyncio.to_thread(
            accept_upload, data, p.covers, state.covers.cache(p.covers)
        )
    except DownloadError as exc:
        raise ApiError(422, "bad_image", str(exc)) from None
    return CoverUploaded(candidate=candidate)


# -- the profile's provider defaults (COVER-PICKER-9, -10) -------------------------------


@router.get(
    "/profiles/{profile}/covers", response_model=CoverProvidersView, responses=ERROR_RESPONSES
)
async def cover_providers(profile: str, state: State) -> CoverProvidersView:
    """The profile's cover providers in order (without their own settings), every
    installed provider, and the hash a save must send back."""
    p = get_profile(state, profile)
    return CoverProvidersView(
        providers=[
            CoverProviderOrder(name=x.name, enabled=x.enabled, weight=x.weight)
            for x in p.covers.providers
            if x.name != UPLOAD_PROVIDER
        ],
        registered=sorted(state.covers.providers),
        profile_hash=profile_file_hash(state.config_root, p.name),
    )


@router.put(
    "/profiles/{profile}/covers/providers",
    response_model=CoverProvidersView,
    responses=ERROR_RESPONSES,
)
async def save_providers(
    profile: str, body: CoverProvidersBody, state: State
) -> CoverProvidersView:
    """Save the toggles, weights and order as ``covers.providers`` in ``profile.yaml``,
    keeping the rest of the file. ``409 conflict`` when the file changed since it was read."""
    p = get_profile(state, profile)
    names = [x.name for x in body.providers]
    if len(set(names)) != len(names):
        raise ApiError(400, "bad_request", "each provider may be listed once")
    unknown = [n for n in names if n not in state.covers.providers]
    if unknown:
        raise ApiError(400, "bad_request", f"no cover provider named {', '.join(unknown)}")
    try:
        await asyncio.to_thread(
            save_cover_providers,
            state.config_root,
            p.name,
            [(x.name, x.enabled, x.weight) for x in body.providers],
            body.profile_hash,
        )
    except ProfileChanged:
        raise ApiError(
            409, "conflict", "profile.yaml changed since it was loaded; load it again"
        ) from None
    except ProfileLoadError as exc:
        raise ApiError(422, "validation_failed", f"the profile would be invalid: {exc}") from None
    except ProfileError as exc:
        raise not_found(str(exc)) from None
    return await cover_providers(profile, state)
