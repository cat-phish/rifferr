"""Step types used only by engine tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from rifferr.backends.base import BackendCapabilities
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


ALL = [EchoStep(), PerAlbumStep(), SlowStep(), CrashStep(), ImportStep()]
