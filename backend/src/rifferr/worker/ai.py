"""The worker's side of ``ctx.ask_ai`` (C-AI-CORE): runs installed AI tasks for steps.

The worker owns the profile, its secrets and the app DB, so the answer cache is shared
with the server's AI service.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter

from rifferr.ai.base import AIProvider, AITask
from rifferr.ai.cache import AppAICache
from rifferr.ai.registry import load_providers, load_tasks
from rifferr.ai.runner import AIRuntime, AITaskError, usage_json
from rifferr.appdb import AppDB
from rifferr.core.profile import Profile
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine.steps import AIAnswer, AIEmit, AIUnavailableError

_ANY: TypeAdapter[Any] = TypeAdapter(Any)


class WorkerAI:
    def __init__(
        self,
        profile: Profile,
        db: AppDB,
        *,
        providers: Mapping[str, AIProvider] | None = None,
        tasks: Mapping[str, AITask[Any, Any]] | None = None,
    ) -> None:
        self.profile = profile
        self.db = db
        self._providers = providers
        self._tasks = tasks

    @property
    def configured(self) -> bool:
        return self.profile.ai.configured

    def _registries(self) -> tuple[Mapping[str, AIProvider], Mapping[str, AITask[Any, Any]]]:
        if self._providers is None:
            self._providers, _ = load_providers()
        if self._tasks is None:
            self._tasks, _ = load_tasks()
        return self._providers, self._tasks

    async def run_task(
        self,
        task: str,
        input: Mapping[str, JsonValue],
        *,
        provider: str | None,
        target: str,
        emit: AIEmit,
        retries: int = 2,
        refresh: bool = False,
    ) -> AIAnswer:
        providers, tasks = self._registries()
        found = tasks.get(task)
        if found is None:
            raise AIUnavailableError(f"AI task '{task}' is not installed")
        runtime = AIRuntime(
            profile=self.profile.name,
            root=Path(self.profile.root),
            settings=self.profile.ai,
            providers=providers,
            cache=AppAICache(self.db),
            secret=ProfileSecrets(self.profile.secrets, SecretResolver()).resolve,
            emit=emit,
        )
        try:
            result = await runtime.run_task(
                found,
                found.input_model.model_validate(input),
                provider=provider,
                target=target,
                retries=retries,
                refresh=refresh,
            )
        except AITaskError as exc:
            raise AIUnavailableError(exc.message) from None
        return AIAnswer(
            value=_ANY.dump_python(result.value, mode="json"),
            rejected=[r.model_dump(mode="json") for r in result.rejected],
            usage=usage_json(result.usage),
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            cached=result.cached,
        )
