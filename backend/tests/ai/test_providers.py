"""Built-in AI providers and the AI HTTP client, with recorded replies (AI-CORE-2 to -4, -21)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from rifferr.ai.base import AIError, AIProviderConfig, AIRequest
from rifferr.ai.http import AIHttp, parse_retry_after
from rifferr.ai.providers.anthropic import AnthropicProvider
from rifferr.ai.providers.openai_compat import OpenAICompatibleProvider
from rifferr.ai.registry import load_providers
from rifferr.ai.testing import FakeAIProvider
from rifferr.core.profile import Redactor, Secret

REPLIES = Path(__file__).parent / "replies"
KEY = Secret("sk-ant-test-0123456789")
REQ = AIRequest(system="sys", user="usr", output_schema={"type": "object"}, max_output_tokens=300)


def reply(name: str) -> Any:
    return json.loads((REPLIES / name).read_text())


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as c:
        yield c


def http(client: httpx.AsyncClient, host: str, **kw: Any) -> AIHttp:
    args: dict[str, Any] = {"timeout": 5.0, "max_bytes": 100_000, "redactor": Redactor([KEY])}
    return AIHttp(client, host, **(args | kw))


ANTHROPIC = AIProviderConfig(name="claude", type="anthropic", model="claude-haiku-5-5")
LOCAL = AIProviderConfig(
    name="local", type="openai-compatible", model="qwen3:14b", base_url="http://127.0.0.1:11434/v1"
)


def test_AI_CORE_1_builtins_register_through_entry_points() -> None:
    found, errors = load_providers()
    assert errors == []
    assert set(found) >= {"anthropic", "openai-compatible"}


def test_AI_CORE_1_broken_plugin_is_reported_not_raised() -> None:
    class EP:
        name, value = "bad", "x:Y"

        def load(self) -> Any:
            raise ImportError("nope")

    class Named:
        name, value = "fake", "x:Fake"

        def load(self) -> Any:
            return FakeAIProvider(type="other")

    found, errors = load_providers(lambda group: [EP(), Named()])
    assert found == {}
    assert "failed to load" in errors[0]
    assert "its type is 'other'" in errors[1]


@respx.mock
async def test_AI_CORE_2_anthropic_forced_tool_call(client: httpx.AsyncClient) -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").respond(
        json=reply("anthropic_ok.json")
    )
    got = await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert got.data == {"suggestions": [{"value": "Alt/Indie", "confidence": 0.8}]}
    assert got.model == "claude-haiku-5-5"
    assert (got.usage.input_tokens, got.usage.output_tokens, got.usage.cost) == (812, 41, None)
    sent = json.loads(route.calls[0].request.content)
    assert sent["tool_choice"] == {"type": "tool", "name": "answer"}
    assert sent["tools"][0]["input_schema"] == {"type": "object"}
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["max_tokens"] == 300
    assert route.calls[0].request.headers["x-api-key"] == KEY.reveal()
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"


@respx.mock
async def test_AI_CORE_2_anthropic_error_is_not_retried_and_hides_key(
    client: httpx.AsyncClient,
) -> None:
    body = reply("anthropic_error.json")
    body["error"]["message"] = f"invalid x-api-key {KEY.reveal()}"  # a server echoing the key
    respx.post("https://api.anthropic.com/v1/messages").respond(401, json=body)
    with pytest.raises(AIError) as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert not exc.value.retryable
    assert "HTTP 401" in exc.value.message
    assert KEY.reveal() not in exc.value.message


@respx.mock
async def test_AI_CORE_2_anthropic_without_tool_call_is_an_invalid_answer(
    client: httpx.AsyncClient,
) -> None:
    body = reply("anthropic_ok.json")
    body["content"] = [{"type": "text", "text": "I think it is rock"}]
    respx.post("https://api.anthropic.com/v1/messages").respond(json=body)
    with pytest.raises(AIError) as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert exc.value.retryable


@respx.mock
async def test_AI_CORE_2_openai_json_schema_response(client: httpx.AsyncClient) -> None:
    route = respx.post("http://127.0.0.1:11434/v1/chat/completions").respond(
        json=reply("openai_ok.json")
    )
    got = await OpenAICompatibleProvider().complete(REQ, LOCAL, None, http(client, "127.0.0.1"))
    assert got.data == {"suggestions": [{"value": "Punk", "confidence": 0.6}]}
    assert (got.usage.input_tokens, got.usage.output_tokens) == (640, 22)
    sent = json.loads(route.calls[0].request.content)
    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["schema"] == {"type": "object"}
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert "authorization" not in route.calls[0].request.headers


@respx.mock
async def test_AI_CORE_2_openai_falls_back_to_function_call(client: httpx.AsyncClient) -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(400, json=reply("openai_unsupported.json")),
            httpx.Response(200, json=reply("openai_tool_call.json")),
        ]
    )
    cfg = AIProviderConfig(
        name="or", type="openai-compatible", model="m", base_url="https://openrouter.ai/api/v1"
    )
    got = await OpenAICompatibleProvider().complete(REQ, cfg, KEY, http(client, "openrouter.ai"))
    assert got.data == {"suggestions": []}
    assert got.usage.cost == Decimal("0.00042")
    second = json.loads(route.calls[1].request.content)
    assert "response_format" not in second
    assert second["tool_choice"] == {"type": "function", "function": {"name": "answer"}}
    assert route.calls[0].request.headers["authorization"] == f"Bearer {KEY.reveal()}"


@respx.mock
async def test_AI_CORE_2_openai_code_fenced_json_is_accepted(client: httpx.AsyncClient) -> None:
    body = reply("openai_ok.json")
    body["choices"][0]["message"]["content"] = '```json\n{"suggestions": []}\n```'
    respx.post("http://127.0.0.1:11434/v1/chat/completions").respond(json=body)
    got = await OpenAICompatibleProvider().complete(REQ, LOCAL, None, http(client, "127.0.0.1"))
    assert got.data == {"suggestions": []}


@respx.mock
async def test_AI_CORE_2_openai_non_json_answer_is_retryable(client: httpx.AsyncClient) -> None:
    body = reply("openai_ok.json")
    body["choices"][0]["message"]["content"] = "Rock, probably."
    respx.post("http://127.0.0.1:11434/v1/chat/completions").respond(json=body)
    with pytest.raises(AIError) as exc:
        await OpenAICompatibleProvider().complete(REQ, LOCAL, None, http(client, "127.0.0.1"))
    assert exc.value.retryable


@respx.mock
async def test_AI_CORE_14_429_with_retry_after_is_retryable(client: httpx.AsyncClient) -> None:
    respx.post("https://api.anthropic.com/v1/messages").respond(
        429, headers={"retry-after": "7"}, json={"error": "slow down"}
    )
    with pytest.raises(AIError) as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert exc.value.retryable
    assert exc.value.retry_after == 7.0


@respx.mock
async def test_AI_CORE_14_5xx_is_retryable(client: httpx.AsyncClient) -> None:
    respx.post("https://api.anthropic.com/v1/messages").respond(529)
    with pytest.raises(AIError) as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert exc.value.retryable


def test_AI_CORE_14_retry_after_date() -> None:
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == 10.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


async def test_AI_CORE_4_only_the_provider_host(client: httpx.AsyncClient) -> None:
    h = http(client, "api.anthropic.com")
    with pytest.raises(AIError, match="not this provider's host"):
        await h.post_json("https://evil.example/v1/messages", {})
    with pytest.raises(AIError, match="only https"):
        await h.post_json("http://api.anthropic.com/v1/messages", {})


@respx.mock
async def test_AI_CORE_4_http_allowed_for_local_host(client: httpx.AsyncClient) -> None:
    respx.post("http://192.168.1.20:11434/v1/x").respond(json={})
    got = await http(client, "192.168.1.20").post_json("http://192.168.1.20:11434/v1/x", {})
    assert got.status == 200


@respx.mock
async def test_AI_CORE_4_redirects_are_refused(client: httpx.AsyncClient) -> None:
    respx.post("https://api.anthropic.com/v1/messages").respond(
        307, headers={"location": "https://evil.example/steal"}
    )
    evil = respx.post("https://evil.example/steal").respond(json={})
    with pytest.raises(AIError, match="redirect") as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert not exc.value.retryable
    assert not evil.called


@respx.mock
async def test_AI_CORE_4_response_size_is_capped(client: httpx.AsyncClient) -> None:
    respx.post("https://api.anthropic.com/v1/messages").respond(text="x" * 5000)
    with pytest.raises(AIError, match="larger than 1000 bytes"):
        await AnthropicProvider().complete(
            REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com", max_bytes=1000)
        )


@respx.mock
async def test_AI_CORE_4_timeout_is_retryable(client: httpx.AsyncClient) -> None:
    respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(AIError) as exc:
        await AnthropicProvider().complete(REQ, ANTHROPIC, KEY, http(client, "api.anthropic.com"))
    assert exc.value.retryable


async def test_AI_CORE_21_fake_provider_scripts_and_records(client: httpx.AsyncClient) -> None:
    fake = FakeAIProvider().script({"a": 1}, AIError("down", retryable=True))
    cfg = AIProviderConfig(name="f", type="fake", model="m")
    got = await fake.complete(REQ, cfg, KEY, http(client, "fake.invalid"))
    assert got.data == {"a": 1}
    with pytest.raises(AIError):
        await fake.complete(REQ, cfg, None, http(client, "fake.invalid"))
    assert [r.user for r in fake.requests] == ["usr", "usr"]
    assert fake.keys == [KEY.reveal(), None]
