"""An ``AIAccess`` for engine tests: the real ``AIRuntime`` with ``FakeAIProvider``, an
in-memory cache and no waits between retries."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import JsonValue, TypeAdapter

from rifferr.ai.base import AITask
from rifferr.ai.cache import MemoryAICache
from rifferr.ai.runner import AIRuntime, AITaskError, usage_json
from rifferr.ai.testing import FakeAIProvider
from rifferr.core.ai import AISettings
from rifferr.engine.steps import AIAnswer, AIEmit, AIUnavailableError

_ANY: TypeAdapter[Any] = TypeAdapter(Any)


class RuntimeAIAccess:
    def __init__(self, root: Path, tasks: Mapping[str, AITask[Any, Any]], **settings: Any) -> None:
        self.root = root
        self.fake = FakeAIProvider()
        self.tasks = dict(tasks)
        self.cache = MemoryAICache()
        self.settings = AISettings.model_validate(
            {"providers": [{"name": "fake", "type": "fake", "model": "m1"}], **settings}
        )

    @property
    def configured(self) -> bool:
        return self.settings.configured

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
        found = self.tasks[task]

        async def no_wait(_: float) -> None:
            return None

        rt = AIRuntime(
            profile="main",
            root=self.root,
            settings=self.settings,
            providers={"fake": self.fake},
            cache=self.cache,
            emit=emit,
            sleep=no_wait,
        )
        try:
            result = await rt.run_task(
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
