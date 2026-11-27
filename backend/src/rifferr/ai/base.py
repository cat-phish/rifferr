"""AI provider and AI task plugin types (C-AI-CORE AI-CORE-1, -3, -8).

Both are public plugin APIs: built-ins register through the same entry point groups
(``rifferr.ai_providers``, ``rifferr.ai_tasks``) as third-party plugins.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from rifferr.core.ai_validate import NO_LOOKALIKES, Lookalikes, Rejection

if TYPE_CHECKING:
    from rifferr.ai.http import AIHttp
    from rifferr.core.profile import Secret


class AIUsage(BaseModel):
    """Token counts and cost; ``None`` when unknown (AI-CORE-3, -15)."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None
    currency: str | None = None
    cost_estimated: bool = False  # computed from price_per_mtok, not reported (AI-CORE-23)


class AIRequest(BaseModel):
    """What a provider sends (AI-CORE-3)."""

    model_config = ConfigDict(frozen=True)

    system: str
    user: str
    output_schema: dict[str, Any]
    max_output_tokens: int = 4096
    temperature: float = 0.0


class AIResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: dict[str, Any]
    raw: str
    model: str
    usage: AIUsage = Field(default_factory=AIUsage)


class AIProviderConfig(BaseModel):
    """One profile provider entry as a provider sees it. Never holds the key."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    model: str
    base_url: str | None = None
    settings: Mapping[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class AIError(Exception):
    """A failed attempt (AI-CORE-14). ``retryable`` decides whether it is tried again."""

    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


@runtime_checkable
class AIProvider(Protocol):
    """An ``rifferr.ai_providers`` entry point (AI-CORE-1). Must not retry by itself."""

    type: str
    default_host: str | None
    default_base_url: str | None

    async def complete(
        self,
        request: AIRequest,
        config: AIProviderConfig,
        api_key: Secret | None,
        http: AIHttp,
    ) -> AIResponse: ...


class TaskContext(BaseModel):
    """What the core hands a task besides its input (AI-CORE-11)."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    files: Mapping[str, str] = Field(default_factory=dict[str, str])  # name -> text
    lookalikes: Lookalikes = NO_LOOKALIKES


class TaskOutcome[Out](BaseModel):
    """A task's validated result: the value and what it dropped."""

    model_config = ConfigDict(frozen=True)

    value: Out
    rejected: list[Rejection] = Field(default_factory=list[Rejection])


@runtime_checkable
class AITask[In: BaseModel, Out](Protocol):
    """An ``rifferr.ai_tasks`` entry point (AI-CORE-8).

    The answer's shape is ``output_model``; its JSON Schema is what the model gets, and the
    core validates every answer against it before ``validate`` sees it (AI-CORE-10).
    """

    id: str
    prompt_version: int
    input_model: type[In]
    output_model: type[BaseModel]

    def sends(self, inp: In) -> list[str]:
        """Kinds of data the request contains, for the safety report and preview."""
        ...

    def files(self, inp: In) -> Mapping[str, str]:
        """Rules and vocabulary files to load: name -> path relative to the profile dir."""
        ...

    def build(self, inp: In, ctx: TaskContext) -> tuple[str, str]:
        """System and user text. Gets only ``inp`` and ``ctx`` (AI-CORE-11)."""
        ...

    def validate(self, answer: Any, inp: In, ctx: TaskContext) -> TaskOutcome[Out]:
        """Check a schema-valid answer (an ``output_model`` instance). Pure."""
        ...


class AITaskResult[Out](BaseModel):
    """What ``run_task`` returns (AI-CORE-9)."""

    model_config = ConfigDict(frozen=True)

    value: Out
    rejected: list[Rejection] = Field(default_factory=list[Rejection])
    usage: AIUsage = Field(default_factory=AIUsage)
    cached: bool = False
    provider: str
    model: str
    prompt_version: int
