"""The ``library.move`` step (S-LIBRARY-MOVE): journaled rename, move and copy.

Destinations come from the backend's own path formats (beets ``paths:``); the step adds
no path syntax. Each album becomes one FileChangeSet through ``ctx.propose_files``,
which then follows the step's ``apply`` policy (default ``review``). The step runs no
user code and needs no trust.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rifferr.backends.base import BackendCapabilities, QueryError, TemplateError
from rifferr.core.changeset import ShowFields, Stamp
from rifferr.core.moves import ArtifactRule, MoveSettings, PathFormat
from rifferr.core.workflow.template_fields import TEMPLATE_EXTRA
from rifferr.engine import StepContext, StepFailure, StepResult

BEETS_PATHS = (
    "https://beets.readthedocs.io/en/stable/reference/config.html#path-format-configuration"
)


class PathParam(BaseModel):
    """One ``paths`` entry: beets query and path format, as in beets' ``paths:``."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="A beets query, or 'default'")
    format: str = Field(
        min_length=1,
        description=f"A beets path format ({BEETS_PATHS})",
        json_schema_extra=TEMPLATE_EXTRA,
    )

    @field_validator("query", "format")
    @classmethod
    def _no_interpolation(cls, value: str) -> str:
        if "{{" in value:  # LIBRARY-MOVE-2: beets syntax only
            raise ValueError("paths use beets query and template syntax; '{{ }}' isn't allowed")
        return value


class LibraryMoveParams(BaseModel):
    """``with:`` of a ``library.move`` step (LIBRARY-MOVE-1)."""

    model_config = ConfigDict(extra="forbid", title="library.move")

    mode: Literal["move", "copy"] = "move"
    paths: list[PathParam] | None = Field(
        default=None,
        min_length=1,
        description="Override the profile's beets paths: for this step; first match wins",
    )
    directory: str | None = Field(
        default=None, description="Destination root instead of beets' directory"
    )
    artifacts: list[str | ArtifactRule] = Field(
        default_factory=lambda: ["*"],
        description="Untracked files that travel with the album: globs, or {match, to}",
    )
    stamp: Stamp | None = None
    show: ShowFields | None = Field(
        default=None,
        description="Fields the reviewer sees with each album: {album: [...], item: [...]}"
        " (LIBRARY-MOVE-17)",
    )

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.paths is not None and not any(p.query == "default" for p in self.paths):
            raise ValueError("paths needs a 'default' entry, as beets' paths: does")
        return self

    def rules(self) -> list[ArtifactRule]:
        return [ArtifactRule(match=[r]) if isinstance(r, str) else r for r in self.artifacts]

    def formats(self) -> list[PathFormat] | None:
        if self.paths is None:
            return None
        return [PathFormat(query=p.query, format=p.format) for p in self.paths]

    def root(self) -> Path | None:
        if self.directory is None:
            return None
        path = Path(self.directory).expanduser()
        if not path.is_absolute():
            raise StepFailure(f"directory must be an absolute path: {self.directory}")
        return path

    def settings(self) -> MoveSettings:
        """The resolved ``with:``, kept with a conflict for its retry (LIBRARY-MOVE-5a)."""
        root = self.root()
        return MoveSettings(
            mode=self.mode,
            formats=self.formats(),
            directory=str(root) if root is not None else None,
            artifacts=self.rules(),
            stamp=self.stamp,
        )


class LibraryMoveStep:
    name = "library.move"
    waits = "always"  # may put items in the review queue (UI-REFRESH-13)
    params_model = LibraryMoveParams
    requires = BackendCapabilities(query=True, move=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, LibraryMoveParams)
        ctx.albums()  # the step's targets (and their snapshots for the stamp)
        settings = params.settings()
        try:
            plans = ctx.backend.plan_moves(
                ctx.selection.model_copy(update={"scope": "album"}),
                mode=settings.mode,
                formats=settings.formats,
                directory=params.root(),
                artifacts=settings.artifacts,
            )
        except (QueryError, TemplateError) as exc:
            raise StepFailure(str(exc)) from exc
        proposed = files = conflicts = 0
        for plan in plans:
            if ctx.cancelled:
                break
            album = plan.album.id
            if plan.conflicts:  # LIBRARY-MOVE-5: never overwrite, never merge
                conflicts += 1
                for line in plan.conflicts:
                    ctx.log(f"{plan.label}: conflict: {line}", "error", album)
                ctx.request_move_conflict(plan, settings)  # LIBRARY-MOVE-5a: lane waits
                continue
            if not plan.files:
                ctx.log(f"{plan.label}: already in place", target=album)
                continue
            ctx.log(
                f"{plan.label}: {params.mode} {len(plan.files)} files: "
                f"{plan.src_dir} -> {plan.dst_dir}",
                target=album,
            )
            try:
                ctx.propose_files(plan, params.stamp, params.show)
            except StepFailure as exc:  # LIBRARY-MOVE-10: this album's lane fails
                ctx.fail_target(album, str(exc))
                continue
            proposed += 1
            files += len(plan.files)
        return StepResult(outputs={"changesets": proposed, "files": files, "conflicts": conflicts})
