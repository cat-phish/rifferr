"""Run AI tasks: the one path every AI feature goes through (AI-CORE-9 to -12, -14 to -18).

Resolve the provider, load the task's files, build the request, look up the cache, call
the provider with retries, validate the answer against the task's output model, then let
the task check it. A task never calls a provider itself.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic import ValidationError as PydanticValidationError

from rifferr.ai.base import (
    AIError,
    AIProvider,
    AIProviderConfig,
    AIRequest,
    AITask,
    AITaskResult,
    AIUsage,
    TaskContext,
    TaskOutcome,
)
from rifferr.ai.cache import AICacheStore, CacheEntry, cache_key
from rifferr.ai.http import AIHttp
from rifferr.core.ai import AINotConfiguredError, AIProviderSetting, AISettings
from rifferr.core.ai_validate import NO_LOOKALIKES, Lookalikes, Rejection
from rifferr.core.covers import parse_size
from rifferr.core.duration import parse_duration
from rifferr.core.profile import Redactor, Secret
from rifferr.core.workflow.yamldoc import YamlSyntaxError, parse

Emit = Callable[[str, dict[str, JsonValue]], None]
CHARS_PER_TOKEN = 4  # a rough estimate for previews; providers report the real count


class AITaskError(Exception):
    """A task that gave no answer: not configured, a bad file, or attempts ran out."""

    def __init__(self, message: str, *, attempts: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.attempts = attempts


class AIPreview(BaseModel):
    """Exactly what a task would send (AI-CORE-18)."""

    model_config = ConfigDict(frozen=True)

    task: str
    provider: str
    provider_type: str
    model: str
    host: str | None
    system: str
    user: str
    output_schema: dict[str, Any]
    sends: list[str]
    cached: bool
    estimated_input_tokens: int
    max_output_tokens: int
    estimated_cost_max: Decimal | None = None  # input estimate + max output, when priced
    currency: str | None = None


@dataclass
class _Prepared:
    setting: AIProviderSetting
    provider: AIProvider
    request: AIRequest
    ctx: TaskContext
    key: str
    host: str | None


def _no_secret(name: str) -> Secret:
    raise AITaskError(f"secret '{name}' is not available")


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _no_emit(type_: str, data: dict[str, JsonValue]) -> None:
    return None


@dataclass
class AIRuntime:
    """Everything ``run_task`` needs, for one profile. Built by the server or worker."""

    profile: str
    root: Path
    settings: AISettings
    providers: Mapping[str, AIProvider]
    cache: AICacheStore
    client: httpx.AsyncClient | None = None
    secret: Callable[[str], Secret] = _no_secret
    emit: Emit = _no_emit
    sleep: Callable[[float], Awaitable[None]] = _sleep
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    # -- files (AI-CORE-12) -----------------------------------------------------

    def read_file(self, rel: str) -> str:
        """A rules or vocabulary file inside the profile directory (AI-CORE-12)."""
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise AITaskError(f"{rel}: must be a path inside the profile directory")
        root = self.root.resolve()
        full = (root / path).resolve()
        if not full.is_relative_to(root):
            raise AITaskError(f"{rel}: leads outside the profile directory")
        if not full.is_file():
            raise AITaskError(f"{rel}: file not found in the profile directory")
        limit = parse_size(self.settings.max_rules_bytes)
        if full.stat().st_size > limit:
            raise AITaskError(f"{rel}: larger than ai.max_rules_bytes ({limit} bytes)")
        try:
            return full.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise AITaskError(f"{rel}: not UTF-8 text") from None

    def lookalikes(self) -> Lookalikes:
        """The profile's lookalike map, or none (AI-CORE-22)."""
        rel = self.settings.lookalikes
        if rel is None:
            return NO_LOOKALIKES
        text = self.read_file(rel)
        try:
            data = parse(text, rel).plain()
            return Lookalikes.model_validate(data or {})
        except YamlSyntaxError as exc:
            raise AITaskError(f"{rel}: {exc.error}") from None
        except PydanticValidationError as exc:
            first = exc.errors(include_url=False)[0]
            raise AITaskError(f"{rel}: {first['msg']}") from None

    # -- preparing --------------------------------------------------------------

    def _prepare[In: BaseModel](
        self, task: AITask[In, Any], inp: In, provider: str | None
    ) -> _Prepared:
        try:
            setting = self.settings.provider(provider)
        except AINotConfiguredError as exc:
            raise AITaskError(str(exc)) from None
        impl = self.providers.get(setting.type)
        if impl is None:
            raise AITaskError(f"AI provider type '{setting.type}' is not installed")
        files = {name: self.read_file(rel) for name, rel in task.files(inp).items()}
        ctx = TaskContext(files=files, lookalikes=self.lookalikes())
        try:
            system, user = task.build(inp, ctx)
        except ValueError as exc:  # the task refuses its input or files, e.g. a bad vocabulary
            raise AITaskError(f"{task.id}: {exc}") from None
        schema = task.output_model.model_json_schema()
        request = AIRequest(
            system=system,
            user=user,
            output_schema=schema,
            max_output_tokens=setting.settings.max_output_tokens,
            temperature=setting.settings.temperature,
        )
        key = cache_key(
            task=task.id,
            prompt_version=task.prompt_version,
            provider_type=setting.type,
            model=setting.model,
            temperature=request.temperature,
            system=system,
            user=user,
            output_schema=schema,
        )
        host = setting.host(impl.default_host)
        return _Prepared(setting, impl, request, ctx, key, host)

    def _ttl(self) -> timedelta:
        return parse_duration(self.settings.cache_ttl)

    def preview[In: BaseModel](
        self, task: AITask[In, Any], inp: In, *, provider: str | None = None
    ) -> AIPreview:
        """What ``run_task`` would send, without contacting anyone (AI-CORE-18)."""
        p = self._prepare(task, inp, provider)
        cached = self.cache.has(self.profile, p.key, now=self.now(), ttl=self._ttl())
        tokens = math.ceil((len(p.request.system) + len(p.request.user)) / CHARS_PER_TOKEN)
        price = p.setting.settings.price_per_mtok
        cost = price.cost(tokens, p.request.max_output_tokens) if price else None
        return AIPreview(
            task=task.id,
            provider=p.setting.name,
            provider_type=p.setting.type,
            model=p.setting.model,
            host=p.host,
            system=p.request.system,
            user=p.request.user,
            output_schema=p.request.output_schema,
            sends=task.sends(inp),
            cached=cached,
            estimated_input_tokens=tokens,
            max_output_tokens=p.request.max_output_tokens,
            estimated_cost_max=cost,
            currency=price.currency if price else None,
        )

    # -- running ----------------------------------------------------------------

    async def run_task[In: BaseModel, Out](
        self,
        task: AITask[In, Out],
        inp: In,
        *,
        provider: str | None = None,
        retries: int = 2,
        refresh: bool = False,
        target: str = "",
    ) -> AITaskResult[Out]:
        """Run ``task`` once for ``inp`` (AI-CORE-9). Raises ``AITaskError`` when there is
        no answer; the caller decides what that means (AI-CORE-14)."""
        p = self._prepare(task, inp, provider)
        base: dict[str, JsonValue] = {
            "task": task.id,
            "target": target,
            "provider": p.setting.name,
            "model": p.setting.model,
        }
        if not refresh:
            hit = self._from_cache(task, inp, p, base)
            if hit is not None:
                return hit
        answer, usage, model = await self._attempts(task, p, base, retries)
        self.cache.put(
            self.profile,
            p.key,
            CacheEntry(
                task=task.id,
                provider_type=p.setting.type,
                model=p.setting.model,
                prompt_version=task.prompt_version,
                answer=answer.model_dump(mode="json"),
            ),
            now=self.now(),
        )
        self.cache.prune(
            self.profile,
            now=self.now(),
            ttl=self._ttl(),
            max_entries=self.settings.cache_max_entries,
        )
        outcome = task.validate(answer, inp, p.ctx)
        self._emit_rejected(base, outcome)
        return AITaskResult[Out](
            value=outcome.value,
            rejected=outcome.rejected,
            usage=usage,
            cached=False,
            provider=p.setting.name,
            model=model,
            prompt_version=task.prompt_version,
        )

    def _from_cache[In: BaseModel, Out](
        self, task: AITask[In, Out], inp: In, p: _Prepared, base: dict[str, JsonValue]
    ) -> AITaskResult[Out] | None:
        entry = self.cache.get(self.profile, p.key, now=self.now(), ttl=self._ttl())
        if entry is None:
            return None
        try:
            answer = task.output_model.model_validate(entry.answer)
        except PydanticValidationError:
            return None  # an entry from an older output model: ask again
        self.emit("ai.request", {**base, "attempt": 0, "cached": True})
        outcome = task.validate(answer, inp, p.ctx)
        self._emit_rejected(base, outcome)
        return AITaskResult[Out](
            value=outcome.value,
            rejected=outcome.rejected,
            usage=AIUsage(input_tokens=0, output_tokens=0, cost=Decimal(0)),
            cached=True,
            provider=p.setting.name,
            model=entry.model,
            prompt_version=task.prompt_version,
        )

    async def _attempts(
        self,
        task: AITask[Any, Any],
        p: _Prepared,
        base: dict[str, JsonValue],
        retries: int,
    ) -> tuple[BaseModel, AIUsage, str]:
        key: Secret | None = None
        if p.setting.api_key:
            try:
                key = self.secret(p.setting.api_key)
            except AITaskError:
                raise
            except Exception as exc:  # e.g. SecretNotAvailableError: names a ref, not a value
                raise AITaskError(f"the API key for '{p.setting.name}': {exc}") from None
        redactor = Redactor([key] if key else [])
        config = AIProviderConfig(
            name=p.setting.name,
            type=p.setting.type,
            model=p.setting.model,
            base_url=p.setting.base_url,
            settings=p.setting.settings.extra(),
        )
        timeout = parse_duration(self.settings.timeout).total_seconds()
        max_wait = parse_duration(self.settings.max_retry_wait).total_seconds()
        max_bytes = parse_size(self.settings.max_response_bytes)
        client = self.client or httpx.AsyncClient()
        try:
            http = AIHttp(
                client, p.host or "", timeout=timeout, max_bytes=max_bytes, redactor=redactor
            )
            attempts = retries + 1
            for attempt in range(1, attempts + 1):
                self.emit("ai.request", {**base, "attempt": attempt, "cached": False})
                started = time.monotonic()
                try:
                    response = await p.provider.complete(p.request, config, key, http)
                    answer = task.output_model.model_validate(response.data)  # AI-CORE-10
                except PydanticValidationError as exc:
                    error = AIError(_schema_problem(exc), retryable=True)
                except AIError as exc:
                    error = exc
                except Exception as exc:  # a provider plugin's bug: never retried
                    message = redactor.redact(f"{type(exc).__name__}: {exc}")
                    raise AITaskError(f"AI provider failed: {message}", attempts=attempt) from None
                else:
                    usage = self._priced(p.setting, response.usage)
                    self.emit(
                        "ai.response",
                        {
                            **base,
                            "attempt": attempt,
                            "usage": usage_json(usage),
                            "duration": round(time.monotonic() - started, 3),
                        },
                    )
                    return answer, usage, response.model
                message = redactor.redact(error.message)
                if not error.retryable or attempt == attempts:
                    raise AITaskError(message, attempts=attempt)
                wait = error.retry_after if error.retry_after is not None else 2.0 ** (attempt - 1)
                await self.sleep(min(wait, max_wait))
            raise AssertionError("unreachable")
        finally:
            if self.client is None:
                await client.aclose()

    def _priced(self, setting: AIProviderSetting, usage: AIUsage) -> AIUsage:
        """Fill in an estimated cost from ``price_per_mtok`` (AI-CORE-23)."""
        price = setting.settings.price_per_mtok
        if usage.cost is not None or price is None:
            return usage
        cost = price.cost(usage.input_tokens, usage.output_tokens)
        if cost is None:
            return usage
        return usage.model_copy(
            update={"cost": cost, "currency": price.currency, "cost_estimated": True}
        )

    def _emit_rejected(self, base: dict[str, JsonValue], outcome: TaskOutcome[Any]) -> None:
        for r in outcome.rejected:
            self.emit("ai.rejected", {**base, **_rejection_json(r)})


def _schema_problem(exc: PydanticValidationError) -> str:
    first = exc.errors(include_url=False)[0]
    where = ".".join(str(x) for x in first["loc"]) or "answer"
    return f"the answer doesn't match the expected shape ({where}: {first['msg']})"


def usage_json(usage: AIUsage) -> dict[str, JsonValue]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost": str(usage.cost) if usage.cost is not None else None,
        "currency": usage.currency,
        "cost_estimated": usage.cost_estimated,
    }


def _rejection_json(r: Rejection) -> dict[str, JsonValue]:
    return {"value": r.value, "reason": r.reason, "field": r.field}


def add_usage(a: AIUsage, b: AIUsage) -> AIUsage:
    """Sum two usages; unknown stays unknown only when both are (AI-CORE-15)."""

    def plus[T: (int, Decimal)](x: T | None, y: T | None) -> T | None:
        if x is None:
            return y
        if y is None:
            return x
        return x + y

    return AIUsage(
        input_tokens=plus(a.input_tokens, b.input_tokens),
        output_tokens=plus(a.output_tokens, b.output_tokens),
        cost=plus(a.cost, b.cost),
        currency=a.currency or b.currency,
        cost_estimated=a.cost_estimated or b.cost_estimated,
    )
