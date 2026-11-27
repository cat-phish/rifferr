"""The ``anthropic`` provider: Messages API, structured output through a forced tool call
(AI-CORE-2). Plain ``httpx`` through ``AIHttp``; no SDK."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from rifferr.ai.base import AIError, AIProviderConfig, AIRequest, AIResponse, AIUsage
from rifferr.ai.http import AIHttp
from rifferr.core.profile import Secret

API_VERSION = "2023-06-01"
TOOL = "answer"


class AnthropicProvider:
    type = "anthropic"
    default_host = "api.anthropic.com"
    default_base_url = "https://api.anthropic.com"

    async def complete(
        self,
        request: AIRequest,
        config: AIProviderConfig,
        api_key: Secret | None,
        http: AIHttp,
    ) -> AIResponse:
        if api_key is None:
            raise AIError("the anthropic provider needs an api_key", retryable=False)
        base = (config.base_url or self.default_base_url).rstrip("/")
        body: dict[str, Any] = {
            "model": config.model,
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
            "tools": [
                {
                    "name": TOOL,
                    "description": "Give your answer in this exact shape.",
                    "input_schema": request.output_schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": TOOL},
        }
        headers = {"x-api-key": api_key.reveal(), "anthropic-version": API_VERSION}
        reply = await http.post_json(f"{base}/v1/messages", body, headers)
        if reply.status >= 400:
            raise http.fail(reply)
        data = reply.json()
        if not isinstance(data, dict):
            raise AIError("the server's answer is not a JSON object", retryable=True)
        answer = _tool_input(data)
        return AIResponse(
            data=answer,
            raw=json.dumps(answer, ensure_ascii=False),
            model=str(data.get("model") or config.model),
            usage=_usage(data.get("usage")),
        )


def _tool_input(data: dict[str, Any]) -> dict[str, Any]:
    for block in data.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            payload = block.get("input")
            if block.get("name") == TOOL and isinstance(payload, dict):
                return payload
    stop = data.get("stop_reason")
    raise AIError(f"the answer has no '{TOOL}' tool call (stop_reason {stop})", retryable=True)


def _usage(raw: Any) -> AIUsage:
    if not isinstance(raw, dict):
        return AIUsage()
    cost: Decimal | None = None
    if isinstance(raw.get("cost"), (int, float, str)):
        try:
            cost = Decimal(str(raw["cost"]))
        except InvalidOperation:
            cost = None
    return AIUsage(
        input_tokens=_int(raw.get("input_tokens")),
        output_tokens=_int(raw.get("output_tokens")),
        cost=cost,
        currency="USD" if cost is not None else None,
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
