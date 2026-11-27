"""The profile's ``ai`` section (C-AI-CORE)."""

from __future__ import annotations

import textwrap
from decimal import Decimal
from pathlib import Path

import pytest

from rifferr.core.ai import AINotConfiguredError, base_url_problem, is_private_host
from rifferr.core.profile import Profile, ProfileLoadError, parse_profile

ROOT = Path("/cfg/profiles/main")

TWO = """\
secrets:
  anthropic_key: keyring:rifferr/anthropic
ai:
  providers:
    - name: claude
      type: anthropic
      model: claude-haiku-5-5
      api_key: anthropic_key
      settings:
        price_per_mtok: {input: 1.00, output: 5.00}
    - name: local
      type: openai-compatible
      base_url: http://192.168.1.20:11434/v1
      model: qwen3:14b
      enabled: false
      settings: {temperature: 0.2, num_ctx: 8192}
"""


def load(text: str) -> Profile:
    return parse_profile(textwrap.dedent(text), "profile.yaml", name="main", root=ROOT, env={})


def errors(text: str) -> list[str]:
    with pytest.raises(ProfileLoadError) as exc:
        load(text)
    return [str(e) for e in exc.value.errors]


def test_AI_CORE_5_providers_load_in_order() -> None:
    ai = load(TWO).ai
    assert [p.name for p in ai.providers] == ["claude", "local"]
    assert ai.configured
    assert ai.provider(None).name == "claude"
    assert ai.providers[1].settings.temperature == 0.2
    assert ai.providers[1].settings.extra() == {"num_ctx": 8192}
    assert ai.concurrency == 2
    assert ai.timeout == "60s"


def test_AI_CORE_5_no_ai_section_means_not_configured() -> None:
    ai = load("backend: beets\n").ai
    assert not ai.configured
    with pytest.raises(AINotConfiguredError, match="AI is not configured for this profile"):
        ai.provider(None)


def test_AI_CORE_5_only_disabled_providers_is_not_configured() -> None:
    ai = load("ai:\n  providers:\n    - {name: a, type: anthropic, model: m, enabled: false}\n").ai
    assert not ai.configured


def test_AI_CORE_5_provider_names_are_unique() -> None:
    errs = errors(
        "ai:\n  providers:\n    - {name: a, type: anthropic, model: m}\n"
        "    - {name: a, type: anthropic, model: n}\n"
    )
    assert any("two providers are named 'a'" in e for e in errs)


def test_AI_CORE_6_literal_api_key_is_an_error_that_hides_it() -> None:
    errs = errors(
        "ai:\n  providers:\n    - {name: a, type: anthropic, model: m, api_key: sk-ant-SECRET}\n"
    )
    assert any("must name an entry in secrets" in e for e in errs)
    assert not any("SECRET" in e or "sk-" in e for e in errs)


def test_AI_CORE_6_credentials_in_settings_are_an_error() -> None:
    errs = errors(
        "ai:\n  providers:\n    - name: a\n      type: anthropic\n      model: m\n"
        "      settings: {x_api_key: sk-ant-SECRET}\n"
    )
    assert any("credentials don't belong in settings" in e for e in errs)
    assert not any("SECRET" in e for e in errs)


def test_AI_CORE_7_named_provider_must_exist_and_be_enabled() -> None:
    ai = load(TWO).ai
    assert ai.provider("claude").model == "claude-haiku-5-5"
    with pytest.raises(AINotConfiguredError, match="disabled"):
        ai.provider("local")
    with pytest.raises(AINotConfiguredError, match="no AI provider named 'nope'"):
        ai.provider("nope")


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        "192.168.1.20",
        "10.0.0.5",
        "172.16.3.4",
        "100.101.102.103",
        "nas",
        "box.local",
        "gpu.lan",
        "ai.home.arpa",
        "llm.tail1234.ts.net",
        "fe80::1",
    ],
)
def test_AI_CORE_4_private_hosts(host: str) -> None:
    assert is_private_host(host)


@pytest.mark.parametrize("host", ["api.anthropic.com", "8.8.8.8", "openrouter.ai", "2001:4860::1"])
def test_AI_CORE_4_public_hosts(host: str) -> None:
    assert not is_private_host(host)


def test_AI_CORE_4_http_only_for_private_hosts() -> None:
    assert base_url_problem("http://127.0.0.1:11434/v1") is None
    assert base_url_problem("https://openrouter.ai/api/v1") is None
    assert "use https://" in (base_url_problem("http://openrouter.ai/api/v1") or "")
    assert base_url_problem("ftp://x/") is not None
    assert "credentials" in (base_url_problem("https://u:p@openrouter.ai/v1") or "")
    errs = errors(
        "ai:\n  providers:\n    - {name: a, type: openai-compatible, model: m, "
        "base_url: 'http://example.com/v1'}\n"
    )
    assert any("use https://" in e for e in errs)


def test_AI_CORE_23_price_is_optional() -> None:
    ai = load(TWO).ai
    price = ai.providers[0].settings.price_per_mtok
    assert price is not None
    assert price.currency == "USD"
    assert price.cost(1_000_000, 200_000) == Decimal("2.00")
    assert price.cost(None, 10) is None
    assert ai.providers[1].settings.price_per_mtok is None


def test_AI_CORE_23_bad_prices_are_errors() -> None:
    base = "ai:\n  providers:\n    - name: a\n      type: anthropic\n      model: m\n"
    assert errors(base + "      settings: {price_per_mtok: {input: -1, output: 1}}\n")
    assert errors(base + "      settings: {price_per_mtok: {input: 1}}\n")
