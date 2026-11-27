"""``FakeAIProvider`` for tests: scripted answers, recorded requests, no network
(AI-CORE-21). Public, so plugin authors can test their AI tasks the same way."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rifferr.ai.base import AIError, AIProviderConfig, AIRequest, AIResponse, AIUsage
from rifferr.ai.http import AIHttp
from rifferr.core.profile import Secret

Scripted = dict[str, Any] | AIError | AIResponse


@dataclass
class FakeAIProvider:
    """Each ``complete`` takes the next scripted item: an answer dict, an ``AIResponse``,
    or an ``AIError`` to raise. Running out of items is a test bug."""

    answers: list[Scripted] = field(default_factory=list[Scripted])
    usage: AIUsage = field(default_factory=lambda: AIUsage(input_tokens=100, output_tokens=20))
    type: str = "fake"
    default_host: str | None = "fake.invalid"
    default_base_url: str | None = "https://fake.invalid"
    requests: list[AIRequest] = field(default_factory=list[AIRequest])
    keys: list[str | None] = field(default_factory=list[str | None])

    def script(self, *items: Scripted) -> FakeAIProvider:
        self.answers.extend(items)
        return self

    async def complete(
        self,
        request: AIRequest,
        config: AIProviderConfig,
        api_key: Secret | None,
        http: AIHttp,
    ) -> AIResponse:
        self.requests.append(request)
        self.keys.append(api_key.reveal() if api_key else None)
        if not self.answers:
            raise AssertionError("FakeAIProvider: no scripted answer left")
        item = self.answers.pop(0)
        if isinstance(item, AIError):
            raise item
        if isinstance(item, AIResponse):
            return item
        return AIResponse(data=item, raw=repr(item), model=config.model, usage=self.usage)


def fake_registry(*providers: FakeAIProvider) -> dict[str, Any]:
    return {p.type: p for p in providers}


def all_text(requests: Iterable[AIRequest]) -> str:
    """Everything sent, for "this was never sent" assertions."""
    return "\n".join(f"{r.system}\n{r.user}" for r in requests)
