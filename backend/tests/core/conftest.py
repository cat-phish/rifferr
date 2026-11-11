from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from rifferr.backends.base import BackendCapabilities
from rifferr.core.workflow import StaticStepRegistry, StepTypeInfo


class FakeShellParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: list[str] | str
    per: Literal["run", "album", "item"] = "album"
    timeout: int = 600


class FakeImportParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    mode: Literal["quiet"] = "quiet"


SHELL = StepTypeInfo(name="shell", params_model=FakeShellParams, requires=BackendCapabilities())
IMPORT = StepTypeInfo(
    name="beets.import", params_model=FakeImportParams, requires=BackendCapabilities(import_=True)
)


@pytest.fixture
def registry() -> StaticStepRegistry:
    return StaticStepRegistry([SHELL, IMPORT])
