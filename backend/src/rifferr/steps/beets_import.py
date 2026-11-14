"""The ``beets.import`` step (S-BEETS-IMPORT; modes ``auto``/``interactive``: F-IMPORT-UI).

Uses only the LibraryBackend protocol: whatever library the profile's backend is.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rifferr.backends.base import BackendCapabilities, ImportOutcome
from rifferr.core.duration import Duration, parse_duration
from rifferr.engine import StepContext, StepFailure, StepResult
from rifferr.engine.steps import ImportOptions


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
    duplicate_action: Literal["skip", "keep", "remove", "merge", "upgrade", "ask"] | None = Field(
        default=None,
        description="'ask' makes a duplicate an import decision in modes auto/interactive "
        "(IMPORT-UI-3); quiet mode skips it",
    )
    incremental: bool | None = None

    def overrides(self) -> dict[str, str | bool]:
        return {k: v for k, v in self.model_dump(by_alias=True).items() if v is not None}


class BeetsImportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="Folder to import")
    mode: Literal["quiet", "auto", "interactive"] = Field(
        default="auto",
        description="quiet: strong matches or quiet_fallback; auto: strong matches, the rest "
        "wait as import decisions; interactive: every album waits (F-IMPORT-UI)",
    )
    set: dict[str, str] = Field(
        default_factory=dict[str, str],
        description="Fields stored on every imported album and its items (beets set_fields)",
    )
    options: ImporterOptions = Field(default_factory=ImporterOptions)
    decision_timeout: Duration | None = Field(
        default=None,
        description="Decide an unanswered import decision with on_timeout after this long "
        "(IMPORT-UI-10); default: wait indefinitely",
    )
    on_timeout: Literal["skip", "asis"] = "skip"

    @model_validator(mode="after")
    def _timeout_pair(self) -> Self:
        if "on_timeout" in self.model_fields_set and self.decision_timeout is None:
            raise ValueError("on_timeout needs a decision_timeout")
        if self.mode == "quiet" and self.decision_timeout is not None:
            raise ValueError("decision_timeout needs mode auto or interactive")
        return self


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
        if params.mode != "quiet" and not ctx.backend.capabilities.import_decisions:
            raise StepFailure(
                f"this profile's backend can't ask about imports; mode '{params.mode}' "
                "needs import decisions (use mode: quiet)"
            )
        loop = asyncio.get_running_loop()

        def report(decision: ImportOutcome) -> None:
            loop.call_soon_threadsafe(_log_decision, ctx, decision)

        result = await asyncio.to_thread(
            ctx.backend.import_folder,
            path,
            params.mode,
            None,
            run_id=ctx.run_id,
            options=params.options.overrides(),
            set_fields=params.set,
            should_stop=lambda: ctx.cancelled,
            on_decision=report,
        )
        how = ImportOptions(
            options=params.options.overrides(),
            set_fields=dict(params.set),
            timeout=parse_duration(params.decision_timeout) if params.decision_timeout else None,
            on_timeout=params.on_timeout,
        )
        for question in result.questions:  # IMPORT-UI-4 .. -6
            ctx.request_import(question, how)
            ctx.log(f"waiting for an import decision: {question.path}")
        return StepResult(
            outputs={
                "imported": len(result.imported),
                "skipped": len(result.skipped),
                "duplicates": len(result.duplicates),
                "decisions": len(result.questions),
            }
        )


def _log_decision(ctx: StepContext, decision: ImportOutcome) -> None:
    reason = f": {decision.reason}" if decision.reason else ""
    ctx.log(f"{decision.outcome} {decision.path}{reason}")
    if decision.outcome == "skipped":
        ctx.emit("run.step.import.skipped", path=decision.path, reason=decision.reason or "")
