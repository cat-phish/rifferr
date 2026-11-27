# ruff: noqa: RUF001  # lookalike characters on purpose (AI-CORE-22)
"""``run_task``: the shared path every AI task takes (AI-CORE-5 to -18, -22, -23)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field, JsonValue

from rifferr.ai.base import AIError, TaskContext, TaskOutcome
from rifferr.ai.cache import AppAICache, MemoryAICache
from rifferr.ai.runner import AIRuntime, AITaskError
from rifferr.ai.testing import FakeAIProvider, all_text
from rifferr.appdb import AppDB
from rifferr.core.ai import AISettings
from rifferr.core.ai_validate import Rejection, Vocabulary, match_vocabulary
from rifferr.core.profile import Secret

KEY = "sk-ant-runner-secret-42"


class PickIn(BaseModel):
    album: str
    rules: str | None = None


class Pick(BaseModel):
    value: str = Field(max_length=100)
    confidence: float = Field(ge=0, le=1)


class PickOut(BaseModel):
    picks: list[Pick]


class PickTask:
    """A toy task: pick genres from a vocabulary."""

    id = "test.pick"
    prompt_version = 1
    input_model = PickIn
    output_model = PickOut

    def __init__(self) -> None:
        self.validated: list[Any] = []

    def sends(self, inp: PickIn) -> list[str]:
        return ["album", *(["rules"] if inp.rules else [])]

    def files(self, inp: PickIn) -> Mapping[str, str]:
        return {"rules": inp.rules} if inp.rules else {}

    def build(self, inp: PickIn, ctx: TaskContext) -> tuple[str, str]:
        system = "Pick genres." + (
            f"\n{ctx.lookalikes.prompt_line()}" if ctx.lookalikes.groups else ""
        )
        return system, f"Album: {inp.album}\n{ctx.files.get('rules', '')}"

    def validate(self, answer: Any, inp: PickIn, ctx: TaskContext) -> TaskOutcome[list[str]]:
        self.validated.append(answer)
        assert isinstance(answer, PickOut)
        vocab = Vocabulary(["Alt／Indie", "Punk"], ctx.lookalikes)
        got, rejected = match_vocabulary([p.value for p in answer.picks], vocab, "strict")
        return TaskOutcome[list[str]](value=[m.value for m in got], rejected=rejected)


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t


def settings(**kw: Any) -> AISettings:
    base: dict[str, Any] = {
        "providers": [
            {
                "name": "fake",
                "type": "fake",
                "model": "m1",
                "api_key": "k",
                "settings": {"price_per_mtok": {"input": 1, "output": 5}},
            }
        ]
    }
    return AISettings.model_validate(base | kw)


class Harness:
    def __init__(self, root: Path, **kw: Any) -> None:
        self.fake = FakeAIProvider()
        self.events: list[tuple[str, dict[str, JsonValue]]] = []
        self.sleeps: list[float] = []
        self.clock = Clock()
        self.cache = MemoryAICache()

        async def sleep(s: float) -> None:
            self.sleeps.append(s)

        self.rt = AIRuntime(
            profile="main",
            root=root,
            settings=settings(**kw),
            providers={"fake": self.fake},
            cache=self.cache,
            secret=lambda name: Secret(KEY),
            emit=lambda t, d: self.events.append((t, d)),
            sleep=sleep,
            now=self.clock,
        )

    def types(self) -> list[str]:
        return [t for t, _ in self.events]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


ANSWER = {
    "picks": [{"value": "alt/indie", "confidence": 0.9}, {"value": "Polka", "confidence": 0.2}]
}


async def test_AI_CORE_9_one_path_build_call_validate(h: Harness) -> None:
    h.fake.script(ANSWER)
    task = PickTask()
    got = await h.rt.run_task(task, PickIn(album="Doolittle"), target="a1")
    assert got.value == ["Alt／Indie"]
    assert [(r.value, r.reason) for r in got.rejected] == [("Polka", "not_in_vocabulary")]
    assert got.provider == "fake"
    assert got.model == "m1"
    assert not got.cached
    req = h.fake.requests[0]
    assert req.user.startswith("Album: Doolittle")
    assert req.output_schema == PickOut.model_json_schema()
    assert h.types() == ["ai.request", "ai.response", "ai.rejected"]
    assert h.events[0][1] == {
        "task": "test.pick", "target": "a1", "provider": "fake", "model": "m1",
        "attempt": 1, "cached": False,
    }  # fmt: skip


async def test_AI_CORE_10_schema_invalid_answer_is_retried_never_validated(h: Harness) -> None:
    bad = {"picks": [{"value": "x", "confidence": 7}]}
    h.fake.script(bad, bad, bad)
    task = PickTask()
    with pytest.raises(AITaskError, match="expected shape") as exc:
        await h.rt.run_task(task, PickIn(album="A"))
    assert exc.value.attempts == 3
    assert task.validated == []
    assert h.cache.rows == {}  # failed attempts are never cached (AI-CORE-17)


async def test_AI_CORE_14_retries_with_backoff_and_retry_after(h: Harness) -> None:
    h.fake.script(
        AIError("HTTP 529", retryable=True),
        AIError("HTTP 429", retryable=True, retry_after=600),
        ANSWER,
    )
    got = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert got.value == ["Alt／Indie"]
    assert h.sleeps == [1.0, 60.0]  # backoff, then Retry-After capped by max_retry_wait


async def test_AI_CORE_14_non_retryable_is_not_retried(h: Harness) -> None:
    h.fake.script(AIError("HTTP 401: bad key", retryable=False))
    with pytest.raises(AITaskError, match="HTTP 401") as exc:
        await h.rt.run_task(PickTask(), PickIn(album="A"), retries=5)
    assert exc.value.attempts == 1
    assert len(h.fake.requests) == 1


async def test_AI_CORE_14_retries_zero_means_one_attempt(h: Harness) -> None:
    h.fake.script(AIError("down", retryable=True))
    with pytest.raises(AITaskError):
        await h.rt.run_task(PickTask(), PickIn(album="A"), retries=0)
    assert h.sleeps == []


async def test_AI_CORE_6_key_never_in_errors_or_events(h: Harness) -> None:
    h.fake.script(AIError(f"HTTP 401 key {KEY} rejected", retryable=False))
    with pytest.raises(AITaskError) as exc:
        await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert KEY not in exc.value.message
    assert KEY not in json.dumps(h.events)
    assert h.fake.keys == [KEY]  # the provider did get it


async def test_AI_CORE_6_provider_bug_is_reported_without_key(h: Harness) -> None:
    class Broken(FakeAIProvider):
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError(f"boom {KEY}")

    h.rt.providers = {"fake": Broken()}
    with pytest.raises(AITaskError, match="AI provider failed") as exc:
        await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert KEY not in exc.value.message


async def test_AI_CORE_5_not_configured(tmp_path: Path) -> None:
    h = Harness(tmp_path, providers=[])
    with pytest.raises(AITaskError, match="AI is not configured for this profile"):
        await h.rt.run_task(PickTask(), PickIn(album="A"))


async def test_AI_CORE_7_unknown_provider_and_missing_type(h: Harness) -> None:
    with pytest.raises(AITaskError, match="no AI provider named 'nope'"):
        await h.rt.run_task(PickTask(), PickIn(album="A"), provider="nope")
    h.rt.providers = {}
    with pytest.raises(AITaskError, match="type 'fake' is not installed"):
        await h.rt.run_task(PickTask(), PickIn(album="A"))


async def test_AI_CORE_12_rules_file_is_read_into_the_request(h: Harness) -> None:
    (h.rt.root / "rules").mkdir()
    (h.rt.root / "rules" / "g.md").write_text("Prefer broad buckets.", encoding="utf-8")
    h.fake.script(ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="A", rules="rules/g.md"))
    assert "Prefer broad buckets." in h.fake.requests[0].user


@pytest.mark.parametrize(
    ("rel", "message"),
    [
        ("../outside.md", "inside the profile directory"),
        ("/etc/passwd", "inside the profile directory"),
        ("rules/missing.md", "not found"),
        ("rules/big.md", "larger than ai.max_rules_bytes"),
        ("rules/latin1.md", "not UTF-8"),
        ("rules/link.md", "outside the profile directory"),
    ],
)
async def test_AI_CORE_12_bad_files_fail_before_any_request(
    tmp_path: Path, rel: str, message: str
) -> None:
    root = tmp_path / "profile"
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "big.md").write_text("x" * 2000)
    (root / "rules" / "latin1.md").write_bytes("caf\xe9".encode("latin-1"))
    (tmp_path / "secret.md").write_text("private")
    os.symlink(tmp_path / "secret.md", root / "rules" / "link.md")
    h = Harness(root, max_rules_bytes="1KB")
    with pytest.raises(AITaskError, match=message):
        await h.rt.run_task(PickTask(), PickIn(album="A", rules=rel))
    assert h.fake.requests == []


async def test_AI_CORE_15_usage_and_rejected_values_are_cut(h: Harness) -> None:
    long = "x" * 500
    h.fake.script({"picks": [{"value": long[:100], "confidence": 0.5}]})
    task = PickTask()
    got = await h.rt.run_task(task, PickIn(album="A"))
    assert got.usage.input_tokens == 100
    assert got.usage.output_tokens == 20
    assert Rejection.of(long, "x").value == "x" * 200
    response = dict(h.events)["ai.response"]
    assert isinstance(response["usage"], dict)
    assert response["usage"]["input_tokens"] == 100


async def test_AI_CORE_23_cost_estimated_from_price(h: Harness) -> None:
    h.fake.script(ANSWER)
    got = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert got.usage.cost == Decimal("0.0002")  # 100 * 1/1M + 20 * 5/1M
    assert got.usage.cost_estimated
    assert got.usage.currency == "USD"


async def test_AI_CORE_23_no_price_no_cost(tmp_path: Path) -> None:
    h = Harness(tmp_path, providers=[{"name": "fake", "type": "fake", "model": "m1"}])
    h.fake.script(ANSWER)
    got = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert got.usage.cost is None
    assert not got.usage.cost_estimated


async def test_AI_CORE_16_cache_hit_makes_no_request(h: Harness) -> None:
    h.fake.script(ANSWER)
    first = await h.rt.run_task(PickTask(), PickIn(album="A"))
    h.events.clear()
    again = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert len(h.fake.requests) == 1
    assert again.cached
    assert again.value == first.value
    assert (again.usage.input_tokens, again.usage.output_tokens) == (0, 0)
    assert h.events[0] == ("ai.request", h.events[0][1])
    assert h.events[0][1]["cached"] is True


async def test_AI_CORE_16_key_covers_input_model_and_prompt_version(h: Harness) -> None:
    h.fake.script(ANSWER, ANSWER, ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="A"))
    await h.rt.run_task(PickTask(), PickIn(album="B"))  # other text
    newer = PickTask()
    newer.prompt_version = 2
    await h.rt.run_task(newer, PickIn(album="A"))
    assert len(h.fake.requests) == 3


async def test_AI_CORE_16_cache_stores_answer_before_task_checks(h: Harness) -> None:
    h.fake.script(ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="A"))
    ((_, (entry, _, _)),) = h.cache.rows.items()
    assert entry.answer == ANSWER  # "Polka" is still there; the task drops it each time


async def test_AI_CORE_17_refresh_skips_and_overwrites(h: Harness) -> None:
    h.fake.script(ANSWER, {"picks": [{"value": "Punk", "confidence": 1}]})
    await h.rt.run_task(PickTask(), PickIn(album="A"))
    fresh = await h.rt.run_task(PickTask(), PickIn(album="A"), refresh=True)
    assert fresh.value == ["Punk"]
    assert not fresh.cached
    again = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert again.cached
    assert again.value == ["Punk"]


async def test_AI_CORE_17_entries_expire(h: Harness) -> None:
    h.fake.script(ANSWER, ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="A"))
    h.clock.t += timedelta(days=91)
    got = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert not got.cached
    assert len(h.fake.requests) == 2


async def test_AI_CORE_17_cache_is_capped(tmp_path: Path) -> None:
    h = Harness(tmp_path, cache_max_entries=2)
    h.fake.script(ANSWER, ANSWER, ANSWER)
    for album in ("A", "B", "C"):
        h.clock.t += timedelta(seconds=1)
        await h.rt.run_task(PickTask(), PickIn(album=album))
    assert len(h.cache.rows) == 2


def test_AI_CORE_17_app_db_cache(tmp_path: Path) -> None:
    from rifferr.ai.cache import CacheEntry

    cache = AppAICache(AppDB(tmp_path / "app.db"))
    now = datetime(2026, 11, 26, tzinfo=UTC)
    ttl = timedelta(days=90)
    entry = CacheEntry(task="t", provider_type="fake", model="m", prompt_version=1, answer={"a": 1})
    for i in range(3):
        cache.put("main", f"k{i}", entry, now=now + timedelta(seconds=i))
    cache.put("other", "k0", entry, now=now)
    assert cache.has("main", "k0", now=now, ttl=ttl)
    got = cache.get("main", "k0", now=now + timedelta(seconds=10), ttl=ttl)
    assert got is not None
    assert got.answer == {"a": 1}
    assert cache.prune("main", now=now + timedelta(seconds=10), ttl=ttl, max_entries=2) == 1
    assert cache.get("main", "k1", now=now, ttl=ttl) is None  # least recently used went
    assert cache.get("main", "k0", now=now, ttl=ttl) is not None
    assert cache.get("other", "k0", now=now, ttl=ttl) is not None  # per profile
    assert cache.get("main", "k2", now=now + timedelta(days=91), ttl=ttl) is None


async def test_AI_CORE_18_preview_sends_nothing(h: Harness) -> None:
    preview = h.rt.preview(PickTask(), PickIn(album="Doolittle"))
    assert h.fake.requests == []
    assert preview.user.startswith("Album: Doolittle")
    assert preview.provider == "fake"
    assert preview.model == "m1"
    assert preview.host == "fake.invalid"
    assert preview.sends == ["album"]
    assert not preview.cached
    assert preview.estimated_input_tokens > 0
    assert preview.estimated_cost_max is not None
    assert preview.currency == "USD"
    h.fake.script(ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="Doolittle"))
    assert h.rt.preview(PickTask(), PickIn(album="Doolittle")).cached
    assert KEY not in preview.model_dump_json()


async def test_AI_CORE_22_lookalikes_file_reaches_task_and_prompt(tmp_path: Path) -> None:
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "look.yaml").write_text(
        'groups:\n  - preferred: "／"\n    variants: ["/", "∕"]\n', encoding="utf-8"
    )
    h = Harness(tmp_path, lookalikes="rules/look.yaml")
    h.fake.script({"picks": [{"value": "Alt∕Indie", "confidence": 0.9}]})
    got = await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert got.value == ["Alt／Indie"]
    assert '"／" instead of "/"' in all_text(h.fake.requests)


async def test_AI_CORE_22_bad_lookalikes_file_fails_before_request(tmp_path: Path) -> None:
    (tmp_path / "look.yaml").write_text('groups:\n  - preferred: "/"\n    variants: ["/"]\n')
    h = Harness(tmp_path, lookalikes="look.yaml")
    with pytest.raises(AITaskError, match="both preferred and a variant"):
        await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert h.fake.requests == []


async def test_AI_CORE_3_request_takes_provider_settings_one_call_per_attempt(
    tmp_path: Path,
) -> None:
    h = Harness(
        tmp_path,
        providers=[
            {
                "name": "fake",
                "type": "fake",
                "model": "m1",
                "settings": {"temperature": 0.3, "max_output_tokens": 512},
            }
        ],
    )
    h.fake.script(AIError("down", retryable=True), ANSWER)
    await h.rt.run_task(PickTask(), PickIn(album="A"))
    assert len(h.fake.requests) == 2  # the core retried; the provider made one call each
    req = h.fake.requests[0]
    assert (req.temperature, req.max_output_tokens) == (0.3, 512)


def test_AI_CORE_8_tasks_load_through_entry_points() -> None:
    from rifferr.ai.registry import load_tasks
    from rifferr.plugins import AI_TASKS

    class EP:
        name, value = "test.pick", "tests:PickTask"

        def load(self) -> Any:
            return PickTask

    class Half:
        name, value = "half", "tests:Half"

        def load(self) -> Any:
            return type("Half", (), {"id": "half"})

    groups: list[str] = []

    def eps(group: str) -> list[Any]:
        groups.append(group)
        return [EP(), Half()]

    found, errors = load_tasks(eps)
    assert groups == [AI_TASKS]
    assert list(found) == ["test.pick"]
    assert "missing prompt_version" in errors[0]


async def test_AI_CORE_11_build_gets_only_input_and_context(h: Harness) -> None:
    seen: list[tuple[Any, TaskContext]] = []

    class Spy(PickTask):
        def build(self, inp: PickIn, ctx: TaskContext) -> tuple[str, str]:
            seen.append((inp, ctx))
            return super().build(inp, ctx)

    h.fake.script(ANSWER)
    preview = h.rt.preview(Spy(), PickIn(album="A"))
    await h.rt.run_task(Spy(), PickIn(album="A"))
    for inp, ctx in seen:
        assert inp == PickIn(album="A")
        assert set(TaskContext.model_fields) == {"files", "lookalikes"}
        assert dict(ctx.files) == {}
    assert h.fake.requests[0].user == preview.user  # everything sent is in the preview
    assert h.fake.requests[0].system == preview.system
