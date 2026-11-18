"""The ``workflow`` step (S-SUBWORKFLOW): call another workflow of the profile.

The engine expands a call into the called workflow's steps when the run is created
(SUBWORKFLOW-6), so a written call step never runs. What does run is the end of a
call whose workflow declares ``outputs:``: it resolves them once the called steps
have finished for its lanes (SUBWORKFLOW-11).
"""

from __future__ import annotations

from rifferr.backends.base import BackendCapabilities
from rifferr.core.workflow import CALL_TYPE, CalledStep, WorkflowCallParams
from rifferr.engine import StepContext, StepResult
from rifferr.engine.run import Scalar
from rifferr.engine.steps import StepFailure


class WorkflowCallStep:
    name = CALL_TYPE
    params_model = WorkflowCallParams
    requires = BackendCapabilities()
    run_level = True  # outputs resolve even in a run with no albums yet (LANES-6)

    async def run(self, ctx: StepContext) -> StepResult:
        step = ctx.step
        if not isinstance(step, CalledStep):
            raise StepFailure(
                f"workflow call '{step.id}' wasn't expanded; workflow steps are expanded "
                "when the run is created"
            )
        outputs: dict[str, Scalar] = {}
        for name, expression in step.outputs.items():
            try:
                outputs[name] = ctx.interpolate(expression)
            except StepFailure as exc:
                ctx.log(f"output '{name}' is not available: {exc}", stream="error")
        return StepResult(outputs=outputs)
