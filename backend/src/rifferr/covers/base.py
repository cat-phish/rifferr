"""The cover provider protocol (S-COVERS-SEARCH COVERS-SEARCH-1).

Register a provider under the ``rifferr.cover_providers`` entry-point group. Built-ins
register there too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Final, Protocol, cast, runtime_checkable

from pydantic import JsonValue

from rifferr.core.covers import CoverCandidate, CoverQuery, Replacement, match_score
from rifferr.covers.http import CoverHttp


@runtime_checkable
class CoverProvider(Protocol):
    """``name`` is how profiles and steps refer to it. ``hosts`` are the only hosts its
    ``http`` client may reach (``.example.org`` allows subdomains). With ``needs`` set,
    the provider needs a secret: the profile names it in the provider's settings as
    ``secret``, and the resolved value reaches ``search`` as ``settings["token"]``.

    ``settings`` are the provider's own settings from the profile, plus the profile's
    matching rules under ``MATCH_KEY``; use ``confidence`` to score names with them."""

    name: str
    hosts: frozenset[str]
    needs: bool

    def search(
        self, query: CoverQuery, settings: Mapping[str, JsonValue], http: CoverHttp
    ) -> AsyncIterator[CoverCandidate]: ...


MATCH_KEY: Final = "_match"


def confidence(
    query: CoverQuery,
    artist: str,
    album: str,
    year: int | None,
    settings: Mapping[str, JsonValue],
) -> float:
    """``match_score`` with the profile's ``covers.normalize`` and ``year_penalty``."""
    rules = cast(dict[str, Any], settings.get(MATCH_KEY) or {})
    extra = [Replacement.model_validate(r) for r in cast(list[Any], rules.get("normalize") or [])]
    penalty = rules.get("year_penalty")
    return match_score(
        query,
        artist,
        album,
        year,
        extra=extra,
        year_penalty=float(penalty) if isinstance(penalty, int | float) else 0.8,
    )
