"""A worker process with FakeBackend and test steps, for supervisor tests.

Run as a script: ``python tests/worker/fake_worker.py --profile p --db path``.
``RIFFERR_TEST_HEARTBEAT`` sets the heartbeat interval in seconds.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pydantic import BaseModel, ConfigDict

from rifferr.backends.base import BackendCapabilities
from rifferr.backends.fake import FakeBackend
from rifferr.engine import StepContext, StepResult, StepTypeRegistry
from rifferr.worker.process import main
from tests.engine.steps import ALL


class BlockParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seconds: float


class BlockStep:
    """Blocks the worker's event loop, like a long synchronous beets call."""

    name = "test.block"
    params_model = BlockParams
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, BlockParams)
        ctx.log("blocking")
        await asyncio.sleep(0.05)  # let the log line go out first
        time.sleep(params.seconds)
        return StepResult()


class NoParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrintStep:
    """Writes to stdout directly, as careless plugins do."""

    name = "test.print"
    params_model = NoParams
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        print("stray output from a plugin", flush=True)
        os.write(1, b"raw write to fd 1\n")
        ctx.log("printed")
        return StepResult()


class PidStep:
    name = "test.pid"
    params_model = NoParams
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        return StepResult(outputs={"pid": os.getpid()})


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1:],
            backend_factory=lambda profile: FakeBackend(),
            registry=StepTypeRegistry([*ALL, BlockStep(), PrintStep(), PidStep()]),
            heartbeat_interval=float(os.environ.get("RIFFERR_TEST_HEARTBEAT", "5")),
        )
    )
