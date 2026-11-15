"""Cover search, cover images and cover review decisions (S-COVERS-SEARCH
COVERS-SEARCH-19 to -22).

Searches run in the server, not the beets worker: they need no library. Their results
stream as ``covers.*`` events on the profile's stream. Images are only ever those of
candidates Rifferr found itself or of the art store; the server never fetches a URL a
client sends.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import APIRouter, Query, Response
from pydantic import JsonValue

from rifferr.api.errors import ERROR_RESPONSES, ApiError, not_found
from rifferr.api.models import (
    CoverDecided,
    CoverDecisionBody,
    CoverSearchBody,
    CoverSearchStarted,
)
from rifferr.api.readonly import READ_ONLY_RESPONSES, ask
from rifferr.api.routes.workflows import get_profile
from rifferr.api.state import ServerState, State
from rifferr.appdb import AppCoverReviews, CoverBusyError, CoverResolvedError
from rifferr.core.covers import SHA256, CoverCandidate, CoverReview, mime_of, sniff_image
from rifferr.core.ids import new_ulid
from rifferr.covers.search import DownloadError, ProviderStatus, download, plan, search
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine.events import profile_stream

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["covers"])

DECIDER: Final = "ui"
ART_TIMEOUT: Final = 30.0
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
    if body.review_id is not None:
        review = _review(state, body.review_id)
        if review.profile != profile.name:
            raise not_found(f"cover review {body.review_id}")
        folder = review.query.folder
    query = body.query.model_copy(update={"folder": folder})
    service = state.covers
    unknown = [n for n in plan(settings, body.providers).order if n not in service.providers]
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
            _emit(state, name, "covers.candidate", {**base, "candidate": c.model_dump(mode="json")})

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
async def album_art(album_id: str, profile: str, state: State) -> Response:
    """The album's current art file (COVERS-SEARCH-22), read through the read-only worker."""
    get_profile(state, profile)
    result = await ask(
        state,
        profile,
        "albums.art",
        {"album_id": album_id},
        timeout=ART_TIMEOUT,
        timeout_code="query_timeout",
    )
    path = result.get("path")
    if not isinstance(path, str):
        raise not_found(f"art of album {album_id}")
    try:
        data = await asyncio.to_thread(Path(path).read_bytes)
    except OSError:
        raise not_found(f"art of album {album_id}") from None
    if sniff_image(data[: 1 << 20]) is None and mime_of(data) is None:
        raise ApiError(502, "bad_image", "the album's art isn't a JPEG or PNG image")
    return _image_response(data)


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
        if found.path is not None and (
            review.query.folder is None or Path(found.path).parent != Path(review.query.folder)
        ):
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
