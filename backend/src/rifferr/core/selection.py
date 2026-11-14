"""Which albums or items a step acts on (BACKEND-5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Scope = Literal["album", "item"]


class Selection(BaseModel):
    """A backend query plus a scope, optionally restricted to one run.

    - ``scope="album"``: the query is matched against album fields; matching
      albums are returned with all their items.
    - ``scope="item"``: the query is matched against item fields; albums that
      have at least one matching item are returned with only those items.
    - ``run_id``: only albums carrying this run's marker (albums the run
      imported or touched). A step's default selection is ``for_run(run_id)``.
    - ``ids``: only the albums (scope ``album``) or items (scope ``item``) with these
      backend ids, e.g. to read the current values of a ChangeSet's targets.
    - ``album_ids``: only albums with these backend ids, in either scope (with
      ``scope="item"``, only their items). A pass of a step uses it to act on its
      lanes' albums only (C-LANES LANES-3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = ""
    scope: Scope = "album"
    run_id: str | None = None
    ids: tuple[str, ...] | None = None
    album_ids: tuple[str, ...] | None = None

    @classmethod
    def for_run(cls, run_id: str) -> Selection:
        return cls(run_id=run_id)
