"""The ``review`` gate step (S-REVIEW): stop the run until someone decides.

The gate shows what the run has done so far to the albums it covers and waits in
the review queue for Continue or Reject; nobody has to be watching (unattended
first). It writes nothing to the library.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from rifferr.backends.base import BackendCapabilities, TemplateError
from rifferr.core.changeset import FieldName
from rifferr.core.duration import Duration, parse_duration
from rifferr.core.review import GateRequest
from rifferr.engine import StepContext, StepResult
from rifferr.engine.steps import GateOptions


class ReviewParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(
        default=None,
        description="Shown for each album: a beets template, e.g. "
        "'Check $albumartist - $album before the files move' "
        "(https://beets.readthedocs.io/en/stable/reference/pathformat.html)",
    )
    show: list[FieldName] = Field(
        default_factory=list[str], description="Fields to show for each album"
    )
    on_reject: Literal["stop", "undo"] = Field(
        default="stop",
        description="On reject, stop the run (stop) or also undo its applied tag changes "
        "(undo). Imports are never undone.",
    )
    timeout: Duration | None = Field(
        default=None, description="Decide automatically after this long, e.g. 3d"
    )
    on_timeout: Literal["continue", "reject"] | None = Field(
        default=None, description="The decision taken once timeout passes (default reject)"
    )

    @model_validator(mode="after")
    def _timeout_pair(self) -> Self:
        if self.on_timeout is not None and self.timeout is None:
            raise ValueError("on_timeout needs a timeout")
        return self


class ReviewStep:
    name = "review"
    params_model = ReviewParams
    requires = BackendCapabilities(query=True, templates=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, ReviewParams)
        albums = ctx.albums()
        if not albums:
            ctx.log("nothing to review")
            return StepResult(outputs={"albums": 0})
        requests: list[GateRequest] = []
        for album in albums:
            message: str | None = None
            if params.message is not None:
                try:
                    message = ctx.backend.evaluate(params.message, album)
                except TemplateError as exc:
                    ctx.fail_target(album.id, f"message: {exc}")
                    continue
            shown = {f: album.fields.get(f) for f in params.show}
            requests.append(GateRequest(album_id=album.id, message=message, shown=shown))
        if not requests:
            return StepResult(outputs={"albums": 0})
        timeout = parse_duration(params.timeout) if params.timeout is not None else None
        options = GateOptions(
            on_reject=params.on_reject,
            timeout=timeout,
            on_timeout=(params.on_timeout or "reject") if timeout is not None else None,
        )
        gate_id = ctx.open_gate(requests, options)
        ctx.log(f"waiting for review of {len(requests)} album(s)")
        return StepResult(outputs={"gate": gate_id, "albums": len(requests)})
