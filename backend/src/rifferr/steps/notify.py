"""The ``notify`` step (S-NOTIFY NOTIFY-11 to -13): send a templated message to the
profile's notification targets, once per pass (``per: run``) or once per album.

Targets and their Apprise URLs belong to the profile; a workflow only names targets,
so untrusted workflows can't reach anywhere the profile owner didn't configure.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rifferr.backends.base import BackendCapabilities, TemplateError
from rifferr.core.notify import Notification, NotifyType, SendResult, review_link, run_link
from rifferr.core.workflow.validation import NOTIFY_TARGET_REF
from rifferr.engine import StepContext, StepResult
from rifferr.engine.steps import StepFailure

TEMPLATE_DOCS = "https://beets.readthedocs.io/en/stable/reference/pathformat.html"


class NotifyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[str] | None = Field(
        default=None,
        description="Notification targets from the profile's notify.targets; default: all",
        json_schema_extra={"x-rifferr-ref": NOTIFY_TARGET_REF},
    )
    title: str = Field(description="Title. With per: album, a beets template")
    body: str = Field(
        description=f"Message. With per: album, a beets template, e.g. "
        f"'$albumartist - $album' ({TEMPLATE_DOCS})"
    )
    type: NotifyType = Field(default="info", description="info, success, warning or failure")
    per: Literal["run", "album"] = Field(
        default="run", description="run: one message per pass; album: one per album"
    )
    link: Literal["run", "review", "none"] = Field(
        default="run", description="Link to the run page, the run's review queue, or none"
    )
    required: bool = Field(
        default=False, description="Fail the step when a target can't be reached"
    )
    skip_empty: bool = Field(
        default=False, description="per: run: send nothing when no album matches when:"
    )


class NotifyStep:
    name = "notify"
    params_model = NotifyParams
    requires = BackendCapabilities(query=True, templates=True)

    @staticmethod
    def run_level(with_: Mapping[str, Any]) -> bool:
        """``per: run`` sends once per pass, with or without albums (LANES-6)."""
        return with_.get("per", "run") == "run"

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, NotifyParams)
        url = self._link(ctx, params)
        results: list[SendResult] = []
        if params.per == "run":
            if params.skip_empty and not ctx.albums():
                ctx.log("no album matches; nothing sent")
                return StepResult(outputs={"sent": "", "failed": "", "messages": 0})
            n = Notification(title=params.title, body=params.body, type=params.type, url=url)
            results += await ctx.notify(n, params.targets)
            messages = 1
        else:
            messages = 0
            for album in ctx.albums():
                try:
                    title = ctx.backend.evaluate(params.title, album)
                    body = ctx.backend.evaluate(params.body, album)
                except TemplateError as exc:
                    ctx.fail_target(album.id, f"template: {exc}")
                    continue
                n = Notification(title=title, body=body, type=params.type, url=url)
                results += await ctx.notify(n, params.targets)
                messages += 1
        sent = sorted({r.target for r in results if r.ok})
        failed = sorted({r.target for r in results if not r.ok})
        for r in results:
            if not r.ok:
                ctx.log(f"notification to {r.target} failed: {r.error}", stream="error")
        if failed and params.required:
            raise StepFailure(f"notification failed for: {', '.join(failed)}")
        return StepResult(
            outputs={"sent": ",".join(sent), "failed": ",".join(failed), "messages": messages}
        )

    @staticmethod
    def _link(ctx: StepContext, params: NotifyParams) -> str | None:
        if params.link == "run":
            return run_link(ctx.base_url, ctx.run_id)
        if params.link == "review":
            return review_link(ctx.base_url, run=ctx.run_id)
        return None
