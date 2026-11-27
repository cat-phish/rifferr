"""Settings routes (F-SETTINGS SETTINGS-1 to -3, -5 to -8)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from rifferr.ai.base import AIError
from rifferr.ai.testing import FakeAIProvider
from rifferr.profile_store import config_root, profile_dir
from tests.api.conftest import Api

SECRET = "sk-test-0123456789"
AI_SECTION = """
ai:
  providers:
    - name: claude  # the default
      type: fake
      model: m1
    - name: nas
      type: fake
      model: m2
      base_url: http://127.0.0.1:11434/v1
"""
WORKFLOW = """\
name: tagging
steps:
  - {id: bucket, uses: tags.choose, with: {custom: [genre], ai: {mode: always}}}
  - {id: kind, uses: tags.choose, with: {custom: [albumkind]}}
"""


class MemoryKeyring:
    def __init__(self, available: bool = True) -> None:
        self.ok = available
        self.entries: dict[tuple[str, str], str] = {}

    def available(self) -> bool:
        return self.ok

    def get(self, service: str, name: str) -> str | None:
        return self.entries.get((service, name))

    def set(self, service: str, name: str, value: str) -> None:
        self.entries[(service, name)] = value

    def delete(self, service: str, name: str) -> None:
        self.entries.pop((service, name), None)


def profile_yaml() -> Any:
    return profile_dir(config_root(), "main") / "profile.yaml"


def configure(api: Api, *, keyring: bool = True) -> tuple[FakeAIProvider, MemoryKeyring]:
    path = profile_yaml()
    path.write_text(path.read_text(encoding="utf-8") + AI_SECTION, encoding="utf-8")
    (profile_dir(config_root(), "main") / "workflows").mkdir(exist_ok=True)
    (profile_dir(config_root(), "main") / "workflows" / "tagging.yaml").write_text(WORKFLOW)
    fake = FakeAIProvider(default_host="api.fake.example")
    service = api.state.ai
    assert service is not None
    service.providers = {"fake": fake}
    store = MemoryKeyring(keyring)
    api.state.keyring = store
    return fake, store


async def get_ai(api: Api) -> dict[str, Any]:
    r = await api.client.get("/api/profiles/main/settings/ai")
    assert r.status_code == 200, r.text
    return r.json()


async def test_SETTINGS_1_sections(api: Api) -> None:
    r = await api.client.get("/api/profiles/main/settings")
    assert r.json() == [{"id": "ai", "title": "AI"}]
    assert (await api.client.get("/api/profiles/main/settings/nope")).status_code == 404


async def test_SETTINGS_2_get_values_schema_hash_and_ai_info(api: Api) -> None:
    configure(api)
    body = await get_ai(api)
    assert [p["name"] for p in body["values"]["providers"]] == ["claude", "nas"]
    assert "concurrency" in body["schema"]["properties"]
    assert len(body["base_hash"]) == 64
    ai = body["ai"]
    assert ai["provider_types"] == ["fake"]
    status = {p["name"]: (p["host"], p["local"], p["key_set"]) for p in ai["providers"]}
    assert status == {
        "claude": ("api.fake.example", False, False),
        "nas": ("127.0.0.1", True, False),
    }
    assert ai["steps"] == [
        {"ref": "tagging/bucket", "workflow": "tagging", "step_id": "bucket", "mode": "always"}
    ]


async def test_SETTINGS_3_put_saves_and_keeps_comments(api: Api) -> None:
    configure(api)
    body = await get_ai(api)
    values = body["values"]
    values["suggest"] = {"mode": "off", "steps": {"tagging/bucket": "unsure"}}
    values["providers"].reverse()
    r = await api.client.put(
        "/api/profiles/main/settings/ai", json={"values": values, "base_hash": body["base_hash"]}
    )
    assert r.status_code == 200, r.text
    text = profile_yaml().read_text(encoding="utf-8")
    assert "- name: claude  # the default" in text
    assert text.index("name: nas") < text.index("name: claude")
    assert "tagging/bucket: unsure" in text
    assert r.json()["base_hash"] != body["base_hash"]
    again = await api.client.put(
        "/api/profiles/main/settings/ai", json={"values": values, "base_hash": body["base_hash"]}
    )
    assert (again.status_code, again.json()["error"]["code"]) == (409, "conflict")


async def test_SETTINGS_2_invalid_values_list_the_fields(api: Api) -> None:
    configure(api)
    body = await get_ai(api)
    before = profile_yaml().read_text(encoding="utf-8")
    values = {**body["values"], "concurrency": 0, "suggest": {"mode": "sometimes"}}
    r = await api.client.put(
        "/api/profiles/main/settings/ai", json={"values": values, "base_hash": body["base_hash"]}
    )
    assert r.status_code == 400
    fields = [d["field"] for d in r.json()["error"]["details"]]
    assert fields == ["concurrency", "suggest.mode"]
    assert profile_yaml().read_text(encoding="utf-8") == before


async def test_SETTINGS_6_key_goes_to_the_keyring_only(
    api: Api, caplog: pytest.LogCaptureFixture
) -> None:
    _, store = configure(api)
    caplog.set_level(logging.DEBUG)
    r = await api.client.put(
        "/api/profiles/main/settings/ai/providers/claude/key", json={"key": SECRET}
    )
    assert r.status_code == 200, r.text
    assert SECRET not in r.text
    assert r.json()["key_set"] is True
    assert store.entries == {("rifferr", "main/claude"): SECRET}
    text = profile_yaml().read_text(encoding="utf-8")
    assert SECRET not in text
    assert "ai-claude: keyring:rifferr/main/claude" in text
    assert "api_key: ai-claude" in text
    status = {p["name"]: p for p in (await get_ai(api))["ai"]["providers"]}
    assert status["claude"]["key_set"] is True
    assert status["claude"]["key_secret"] == "ai-claude"
    r = await api.client.request(
        "DELETE", "/api/profiles/main/settings/ai/providers/claude/key", json={}
    )
    assert r.status_code == 200, r.text
    assert r.json()["key_set"] is False
    assert store.entries == {}
    assert "ai-claude" not in profile_yaml().read_text(encoding="utf-8")
    assert SECRET not in caplog.text


async def test_SETTINGS_6_without_a_keyring_say_to_use_the_environment(api: Api) -> None:
    configure(api, keyring=False)
    r = await api.client.put(
        "/api/profiles/main/settings/ai/providers/nas/key", json={"key": SECRET}
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "no_keyring"
    assert err["details"]["env_line"] == "secrets: {ai-nas: env:RIFFERR_MAIN_NAS_KEY}"
    assert SECRET not in r.text
    assert (await get_ai(api))["ai"]["keyring"] is False
    missing = await api.client.put(
        "/api/profiles/main/settings/ai/providers/nope/key", json={"key": SECRET}
    )
    assert missing.status_code == 404


async def test_SETTINGS_7_test_connection(api: Api) -> None:
    fake, _ = configure(api)
    await api.client.put(
        "/api/profiles/main/settings/ai/providers/claude/key", json={"key": SECRET}
    )
    fake.script({"ok": True}, AIError(f"HTTP 401: bad key {SECRET}", retryable=False))
    r = await api.client.post("/api/profiles/main/settings/ai/providers/claude/test", json={})
    assert r.json() == {"ok": True, "provider": "claude", "model": "m1", "error": None}
    assert fake.requests[0].user == 'Answer with the JSON {"ok": true}.'  # no album data
    r = await api.client.post("/api/profiles/main/settings/ai/providers/claude/test", json={})
    body = r.json()
    assert body["ok"] is False
    assert SECRET not in r.text
    assert "bad key" in body["error"]


async def test_SETTINGS_8_mutating_routes_are_json_only_and_origin_checked(api: Api) -> None:
    configure(api)
    body = await get_ai(api)
    r = await api.client.put(
        "/api/profiles/main/settings/ai",
        content=b"values=1",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415
    r = await api.client.put(
        "/api/profiles/main/settings/ai/providers/claude/key",
        json={"key": SECRET},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403
    async with httpx.AsyncClient(
        transport=api.client._transport,  # pyright: ignore[reportPrivateUsage]
        base_url=str(api.client.base_url),
        headers={"origin": str(api.client.base_url).rstrip("/")},
    ) as anon:
        r = await anon.put(
            "/api/profiles/main/settings/ai",
            json={"values": body["values"], "base_hash": body["base_hash"]},
        )
    assert r.status_code == 401
