"""The ``beets.import`` step (S-BEETS-IMPORT), quiet mode only in M0.

Uses only the LibraryBackend protocol: whatever library the profile's backend is.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rifferr.backends.base import BackendCapabilities, ImportDecision
from rifferr.engine import StepContext, StepFailure, StepResult


class ImporterOptions(BaseModel):
    """beets importer options a step may override for its own import (BEETS-IMPORT-4).
    Unset options come from the user's beets config. See
    https://beets.readthedocs.io/en/stable/reference/config.html#importer-options"""

    model_config = ConfigDict(extra="forbid", validate_by_name=True, validate_by_alias=True)

    copy_: bool | None = Field(default=None, alias="copy")  # "copy" would shadow BaseModel.copy
    move: bool | None = None
    link: bool | None = None
    hardlink: bool | None = None
    reflink: bool | Literal["auto"] | None = None
    write: bool | None = None
    autotag: bool | None = None
    quiet_fallback: Literal["skip", "asis"] | None = None
    duplicate_action: Literal["skip", "keep", "remove", "merge", "upgrade"] | None = None
    incremental: bool | None = None

    def overrides(self) -> dict[str, str | bool]:
        return {k: v for k, v in self.model_dump(by_alias=True).items() if v is not None}


class BeetsImportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Folder to import")
    mode: Literal["quiet"] = "quiet"
    set: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Fields stored on every imported album and its items (beets set_fields)",
    )
    options: ImporterOptions = Field(default_factory=ImporterOptions)

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: object) -> object:
        if value in ("auto", "interactive"):
            raise ValueError(f"mode '{value}' arrives with import decisions in M2; use 'quiet'")
        return value


class BeetsImportStep:
    name = "beets.import"
    run_level = True  # LANES-6: brings the albums in, so runs before any lane exists
    params_model = BeetsImportParams
    requires = BackendCapabilities(import_=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, BeetsImportParams)
        path = Path(params.path)
        if not path.exists():
            raise StepFailure(f"import folder {path} does not exist")
        if not path.is_dir():
            raise StepFailure(f"import path {path} is not a folder")
        loop = asyncio.get_running_loop()

        def report(decision: ImportDecision) -> None:
            loop.call_soon_threadsafe(_log_decision, ctx, decision)

        result = await asyncio.to_thread(
            ctx.backend.import_folder,
            path,
            "quiet",
            None,
            run_id=ctx.run_id,
            options=params.options.overrides(),
            set_fields=params.set,
            should_stop=lambda: ctx.cancelled,
            on_decision=report,
        )
        return StepResult(
            outputs={
                "imported": len(result.imported),
                "skipped": len(result.skipped),
                "duplicates": len(result.duplicates),
            }
        )


def _log_decision(ctx: StepContext, decision: ImportDecision) -> None:
    reason = f": {decision.reason}" if decision.reason else ""
    ctx.log(f"{decision.outcome} {decision.path}{reason}")
    if decision.outcome == "skipped":
        ctx.emit("run.step.import.skipped", path=decision.path, reason=decision.reason or "")
