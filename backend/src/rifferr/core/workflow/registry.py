"""What workflow validation needs to know about step types.

C-RUN's step registry (entry points) provides this; tests and the schema
exporter use ``StaticStepRegistry``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from rifferr.backends.base import BackendCapabilities


@dataclass(frozen=True)
class StepTypeInfo:
    name: str
    params_model: type[BaseModel]
    requires: BackendCapabilities


class StepRegistry(Protocol):
    def get(self, name: str) -> StepTypeInfo | None: ...
    def names(self) -> list[str]: ...


class StaticStepRegistry:
    def __init__(self, types: Iterable[StepTypeInfo]) -> None:
        self._types = {t.name: t for t in types}

    def get(self, name: str) -> StepTypeInfo | None:
        return self._types.get(name)

    def names(self) -> list[str]:
        return sorted(self._types)
