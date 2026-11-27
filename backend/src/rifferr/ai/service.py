"""Server-side AI service: registries, per-profile runtimes and jobs (C-AI-CORE).

Features ask it for an ``AIRuntime`` for a profile; it never runs beets and needs no
worker, because a task gets its input from the calling feature.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from rifferr.ai.base import AIProvider, AITask
from rifferr.ai.cache import AICacheStore
from rifferr.ai.jobs import AIJobs, JobEmit
from rifferr.ai.registry import load_providers, load_tasks
from rifferr.ai.runner import AIRuntime
from rifferr.core.profile import Profile

SecretsFor = Callable[[Profile], Callable[[str], Any]]


def _no_emit(profile: str, type_: str, data: dict[str, JsonValue]) -> None:
    return None


@dataclass
class AIService:
    cache: AICacheStore
    load_profile: Callable[[str], Profile]
    secrets_for: SecretsFor
    emit: JobEmit = _no_emit
    providers: dict[str, AIProvider] | None = None
    tasks: dict[str, AITask[Any, Any]] | None = None
    errors: list[str] = field(default_factory=list[str])
    jobs: AIJobs = field(default_factory=AIJobs)

    def __post_init__(self) -> None:
        if self.providers is None:
            self.providers, errors = load_providers()
            self.errors.extend(errors)
        if self.tasks is None:
            self.tasks, errors = load_tasks()
            self.errors.extend(errors)
        self.jobs.emit = self.emit

    def task(self, task_id: str) -> AITask[Any, Any] | None:
        return (self.tasks or {}).get(task_id)

    def runtime(self, profile_name: str) -> AIRuntime:
        profile = self.load_profile(profile_name)
        emit = self.emit
        return AIRuntime(
            profile=profile.name,
            root=Path(profile.root),
            settings=profile.ai,
            providers=self.providers or {},
            cache=self.cache,
            secret=self.secrets_for(profile),
            emit=lambda type_, data: emit(profile.name, type_, data),
        )
