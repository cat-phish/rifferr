"""The profile's ``ai`` section: providers and limits (C-AI-CORE AI-CORE-4 to -7, -23).

Pure: no network, no filesystem. Provider *types* are entry points, checked where the
registry is loaded; this module only checks the shape and the rules that need the rest of
the profile (secret names).
"""

from __future__ import annotations

import ipaddress
import re
from decimal import Decimal
from typing import Annotated, Final, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PositiveInt, field_validator

from rifferr.core.covers import ByteSize
from rifferr.core.duration import Duration

PROVIDER_NAME_PATTERN: Final = r"^[a-z][a-z0-9_-]*$"
ProviderName = Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN, max_length=64)]

# A credential pasted into ``settings`` instead of ``api_key`` (AI-CORE-6).
_CREDENTIAL_KEY: Final = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")

# Host names that only resolve inside a private network (AI-CORE-4). IP literals are
# classified with ``ipaddress``; anything else must use https.
_PRIVATE_SUFFIXES: Final = (".local", ".lan", ".home.arpa", ".internal", ".ts.net")
_CGNAT: Final = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and other overlay networks


class PricePerMTok(BaseModel):
    """Optional prices per million tokens, for cost estimates (AI-CORE-23)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input: Decimal = Field(ge=0)
    output: Decimal = Field(ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")

    def cost(self, input_tokens: int | None, output_tokens: int | None) -> Decimal | None:
        if input_tokens is None or output_tokens is None:
            return None
        million = Decimal(1_000_000)
        return (self.input * input_tokens + self.output * output_tokens) / million


class ProviderSettings(BaseModel):
    """A provider's ``settings``. Known keys are typed; the rest pass to the provider."""

    model_config = ConfigDict(extra="allow", frozen=True)

    temperature: float = Field(default=0.0, ge=0, le=2)
    max_output_tokens: PositiveInt = 4096
    price_per_mtok: PricePerMTok | None = None

    def extra(self) -> dict[str, JsonValue]:
        """Provider-specific settings (everything not typed above)."""
        return dict(self.model_extra or {})


class AIProviderSetting(BaseModel):
    """One entry of ``ai.providers`` (AI-CORE-5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ProviderName
    type: str = Field(min_length=1, description="An installed rifferr.ai_providers entry point")
    model: str = Field(min_length=1)
    base_url: str | None = Field(default=None, description="Server URL; default: the provider's")
    api_key: str | None = Field(
        default=None, description="Name of an entry in secrets; never the key itself"
    )
    enabled: bool = True
    settings: ProviderSettings = Field(default_factory=ProviderSettings)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str | None) -> str | None:
        if value is not None:
            problem = base_url_problem(value)
            if problem:
                raise ValueError(problem)
        return value

    def host(self, default_host: str | None) -> str | None:
        """The one host this provider may contact (AI-CORE-4)."""
        if self.base_url is not None:
            return urlsplit(self.base_url).hostname
        return default_host


SuggestMode = Literal["off", "unsure", "always"]
STEP_REF_PATTERN: Final = r"^[^/\s]+/[^/\s]+$"


class SuggestSettings(BaseModel):
    """``ai.suggest``: when choice reviews ask the AI by themselves (AI-SUGGEST-7)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: SuggestMode = Field(default="unsure", description="Used when nothing else is set")
    steps: dict[Annotated[str, Field(pattern=STEP_REF_PATTERN)], SuggestMode] = Field(
        default_factory=dict[str, SuggestMode],
        description='Overrides per "<workflow>/<step id>"; win over the step\'s with.ai.mode',
    )

    def mode_for(self, workflow: str, step_id: str, step_mode: SuggestMode | None) -> SuggestMode:
        """Settings override, then the step's own mode, then the profile mode."""
        override = self.steps.get(f"{workflow}/{step_id}")
        if override is not None:
            return override
        return step_mode if step_mode is not None else self.mode


class AISettings(BaseModel):
    """``ai`` in ``profile.yaml`` (AI-CORE-5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    concurrency: PositiveInt = 2
    timeout: Duration = "60s"
    max_response_bytes: ByteSize = "1MB"
    max_rules_bytes: ByteSize = "64KB"
    max_retry_wait: Duration = "60s"
    cache_ttl: Duration = "90d"
    cache_max_entries: PositiveInt = 50_000
    lookalikes: str | None = Field(
        default=None, description="Lookalike characters file, relative to the profile directory"
    )
    providers: list[AIProviderSetting] = Field(
        default_factory=list[AIProviderSetting],
        description="In order; the first enabled one is the default",
    )
    suggest: SuggestSettings = Field(default_factory=SuggestSettings)

    @field_validator("providers")
    @classmethod
    def _unique_names(cls, providers: list[AIProviderSetting]) -> list[AIProviderSetting]:
        seen: set[str] = set()
        for p in providers:
            if p.name in seen:
                raise ValueError(f"two providers are named '{p.name}'")
            seen.add(p.name)
        return providers

    @property
    def configured(self) -> bool:
        return any(p.enabled for p in self.providers)

    def default_provider(self) -> AIProviderSetting | None:
        return next((p for p in self.providers if p.enabled), None)

    def provider(self, name: str | None) -> AIProviderSetting:
        """The named provider, or the default (AI-CORE-7). Raises ``AINotConfigured``."""
        if name is None:
            found = self.default_provider()
            if found is None:
                raise AINotConfiguredError(NOT_CONFIGURED)
            return found
        for p in self.providers:
            if p.name == name:
                if not p.enabled:
                    raise AINotConfiguredError(f"AI provider '{name}' is disabled in this profile")
                return p
        raise AINotConfiguredError(f"this profile has no AI provider named '{name}'")

    def checked(self, secret_names: set[str]) -> list[tuple[tuple[str | int, ...], str]]:
        """Problems that need the rest of the profile (AI-CORE-6). Never repeats a value."""
        problems: list[tuple[tuple[str | int, ...], str]] = []
        for i, p in enumerate(self.providers):
            if p.api_key is not None and p.api_key not in secret_names:
                problems.append(
                    (
                        ("ai", "providers", i, "api_key"),
                        "must name an entry in secrets that holds the API key, not the key itself",
                    )
                )
            for key in p.settings.extra():
                if _CREDENTIAL_KEY.search(key):
                    problems.append(
                        (
                            ("ai", "providers", i, "settings", key),
                            "credentials don't belong in settings; put the key in secrets "
                            "and name it in api_key",
                        )
                    )
        return problems


NOT_CONFIGURED: Final = "AI is not configured for this profile"


class AINotConfiguredError(Exception):
    """No usable provider (AI-CORE-5, -7)."""


def is_private_host(host: str) -> bool:
    """Loopback, private-network or overlay-network host (AI-CORE-4)."""
    name = host.strip("[]").lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return name == "localhost" or "." not in name or name.endswith(_PRIVATE_SUFFIXES)
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local


def base_url_problem(url: str) -> str | None:
    """Why ``url`` can't be a provider's ``base_url``, or ``None`` (AI-CORE-4)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "base_url must be an http:// or https:// URL with a host"
    if parts.username or parts.password:
        return "base_url must not contain credentials; put the key in secrets"
    if parts.query or parts.fragment:
        return "base_url must not have a query or fragment"
    if parts.scheme == "http" and not is_private_host(parts.hostname):
        return "http:// is allowed only for local and private-network hosts; use https://"
    return None
