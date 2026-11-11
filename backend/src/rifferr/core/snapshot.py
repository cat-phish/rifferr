"""Snapshots: the engine's read-only view of albums and items (BACKEND-3, -4).

Snapshots are plain, immutable data. The same JSON is published as a schema and
fed to ``shell`` steps on stdin, so the shape is public API.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: A tag value, typed as beets stores it. Dates stay strings or ints (year,
#: month, day), as in beets.
FieldValue = (
    Annotated[bool, Field(strict=True)]
    | Annotated[int, Field(strict=True)]
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
    | str
    | list[str]
    | None
)


class ItemSnapshot(BaseModel):
    """One track. ``id`` is opaque and assigned by the backend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    path: str
    fields: dict[str, FieldValue]


class AlbumSnapshot(BaseModel):
    """An album with its items in track order (disc, then track)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    path: str | None
    fields: dict[str, FieldValue]
    items: list[ItemSnapshot] = Field(default_factory=list[ItemSnapshot])
