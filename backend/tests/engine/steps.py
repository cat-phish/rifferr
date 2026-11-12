"""Step types used only by engine tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from rifferr.backends.base import BackendCapabilities
from rifferr.core.changeset import ChangeSetProposal, TargetRef
from rifferr.engine import StepContext, StepFailure, StepResult


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EchoParams(_P):
    message: str = ""
    fail: bool = False
    outputs: dict[str, str | int | bool] = {}


class EchoStep:
    name = "test.echo"
    params_model = EchoParams
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, EchoParams)
        ctx.log(params.message)
        if params.fail:
            raise StepFailure(f"echo failed: {params.message}")
        return StepResult(outputs=dict(params.outputs))


class PerAlbumParams(_P):
    fail_on: list[str] = []
    scope: str = "album"


class PerAlbumStep:
    name = "test.per-album"
    params_model = PerAlbumParams
    requires = BackendCapabilities(query=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, PerAlbumParams)
        done = 0
        for album in ctx.albums():
            name = str(album.fields.get("album"))
            if name in params.fail_on:
                ctx.fail_target(album.id, f"could not process {name}")
                continue
            ctx.log(f"processed {name}")
            done += 1
        return StepResult(outputs={"done": done})


class SlowParams(_P):
    cooperative: bool = True


class SlowStep:
    name = "test.slow"
    params_model = SlowParams
    requires = BackendCapabilities()
    started: asyncio.Event | None = None

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, SlowParams)
        if SlowStep.started is not None:
            SlowStep.started.set()
        if params.cooperative:
            while not ctx.cancelled:
                await asyncio.sleep(0.005)
            ctx.log("stopping cleanly")
            return StepResult()
        await asyncio.sleep(3600)
        return StepResult()


class CrashStep:
    name = "test.crash"
    params_model = _P
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        raise RuntimeError("unexpected bug")


class ImportParams(_P):
    path: str


class ImportStep:
    name = "test.import"
    params_model = ImportParams
    requires = BackendCapabilities(import_=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, ImportParams)
        result = ctx.backend.import_folder(Path(params.path), "quiet", None, run_id=ctx.run_id)
        return StepResult(outputs={"imported": len(result.imported)})


class ProposeParams(_P):
    set: dict[str, Any] = {}
    """Short form per album: field -> new value."""
    old: dict[str, Any] = {}
    """Explicit ``old`` values for ``set`` fields."""
    delete: list[str] = []
    item_set: dict[str, Any] = {}
    """Field -> new value for every item, as explicit item targets."""
    whole_run: bool = False
    """One proposal for all albums (``set`` + ``item_set``), split by the engine."""
    raw: dict[str, Any] | None = None
    """A literal proposal, proposed once with no current target."""
    rationale: str | None = None


class ProposeStep:
    name = "test.propose"
    params_model = ProposeParams
    requires = BackendCapabilities(query=True, apply=True)

    async def run(self, ctx: StepContext) -> StepResult:
        p = ctx.params
        assert isinstance(p, ProposeParams)
        albums = ctx.albums()
        if p.raw is not None:
            ids = ctx.propose(ChangeSetProposal.model_validate(p.raw))
            return StepResult(outputs={"proposed": len(ids)})

        def album_changes() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for field, new in p.set.items():
                change: dict[str, Any] = {"field": field, "new": new}
                if field in p.old:
                    change["old"] = p.old[field]
                out.append(change)
            out += [{"field": f, "op": "delete"} for f in p.delete]
            return out

        def targets(album: Any) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            if album_changes():
                out.append(
                    {"target": {"scope": "album", "id": album.id}, "changes": album_changes()}
                )
            if p.item_set:
                for item in album.items:
                    out.append(
                        {
                            "target": {"scope": "item", "id": item.id},
                            "changes": [{"field": f, "new": v} for f, v in p.item_set.items()],
                        }
                    )
            return out

        ids: list[str] = []
        if p.whole_run:
            every = [t for a in albums for t in targets(a)]
            if every:
                ids = ctx.propose(
                    ChangeSetProposal.model_validate({"targets": every, "rationale": p.rationale})
                )
        else:
            for album in albums:
                if p.item_set:
                    proposal = {"targets": targets(album), "rationale": p.rationale}
                    ids += ctx.propose(ChangeSetProposal.model_validate(proposal))
                elif album_changes():
                    proposal = {"changes": album_changes(), "rationale": p.rationale}
                    ids += ctx.propose(
                        ChangeSetProposal.model_validate(proposal),
                        current=TargetRef(scope="album", id=album.id),
                    )
        return StepResult(outputs={"proposed": len(ids)})


class ReviewOnlyProposeStep(ProposeStep):
    name = "test.propose-reviewed"
    force_review = True


ALL = [
    EchoStep(),
    PerAlbumStep(),
    SlowStep(),
    CrashStep(),
    ImportStep(),
    ProposeStep(),
    ReviewOnlyProposeStep(),
]
