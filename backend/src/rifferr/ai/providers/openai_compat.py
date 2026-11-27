"""The ``openai-compatible`` provider: Chat Completions with a JSON-schema response format,
falling back to a forced function call when the server doesn't support
``response_format`` (AI-CORE-2). Covers OpenAI, Ollama, LM Studio and OpenRouter."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from rifferr.ai.base import AIError, AIProviderConfig, AIRequest, AIResponse, AIUsage
from rifferr.ai.http import AIHttp, AIReply
from rifferr.core.profile import Secret

FUNCTION = "answer"
# A 400/422 whose text says the server doesn't understand structured output.
_UNSUPPORTED = re.compile(r"(?i)response_format|json_schema|structured")


class OpenAICompatibleProvider:
    type = "openai-compatible"
    default_host = "api.openai.com"
    default_base_url = "https://api.openai.com/v1"

    async def complete(
        self,
        request: AIRequest,
        config: AIProviderConfig,
        api_key: Secret | None,
        http: AIHttp,
    ) -> AIResponse:
        base = (config.base_url or self.default_base_url).rstrip("/")
        url = f"{base}/chat/completions"
        headers = {"authorization": f"Bearer {api_key.reveal()}"} if api_key else {}
        common: dict[str, Any] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
        }
        body = {
            **common,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": FUNCTION, "schema": request.output_schema},
            },
        }
        reply = await http.post_json(url, body, headers)
        if reply.status in (400, 422) and _UNSUPPORTED.search(reply.text):
            body = {
                **common,
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": FUNCTION, "parameters": request.output_schema},
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": FUNCTION}},
            }
            reply = await http.post_json(url, body, headers)
        if reply.status >= 400:
            raise http.fail(reply)
        return _parse(reply, config)


def _parse(reply: AIReply, config: AIProviderConfig) -> AIResponse:
    data = reply.json()
    if not isinstance(data, dict):
        raise AIError("the server's answer is not a JSON object", retryable=True)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIError("the answer has no choices", retryable=True)
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise AIError("the answer has no message", retryable=True)
    text = _function_arguments(message)
    if text is None:
        content = message.get("content")
        text = content if isinstance(content, str) else None
    if text is None:
        raise AIError("the answer has no content", retryable=True)
    try:
        answer = json.loads(_strip_fence(text))
    except ValueError:
        raise AIError("the model's answer is not JSON", retryable=True) from None
    if not isinstance(answer, dict):
        raise AIError("the model's answer is not a JSON object", retryable=True)
    return AIResponse(
        data=answer,
        raw=text,
        model=str(data.get("model") or config.model),
        usage=_usage(data.get("usage")),
    )


def _function_arguments(message: dict[str, Any]) -> str | None:
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict) and fn.get("name") == FUNCTION:
            args = fn.get("arguments")
            if isinstance(args, str):
                return args
            if isinstance(args, dict):
                return json.dumps(args)
    return None


def _strip_fence(text: str) -> str:
    """Local models sometimes wrap JSON in a Markdown code fence."""
    m = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text, re.DOTALL)
    return m.group(1) if m else text


def _usage(raw: Any) -> AIUsage:
    if not isinstance(raw, dict):
        return AIUsage()
    cost: Decimal | None = None
    if isinstance(raw.get("cost"), (int, float, str)) and not isinstance(raw.get("cost"), bool):
        try:  # OpenRouter reports the cost in credits (USD)
            cost = Decimal(str(raw["cost"]))
        except InvalidOperation:
            cost = None
    return AIUsage(
        input_tokens=_int(raw.get("prompt_tokens")),
        output_tokens=_int(raw.get("completion_tokens")),
        cost=cost,
        currency="USD" if cost is not None else None,
    )


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
