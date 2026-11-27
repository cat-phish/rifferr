# ruff: noqa: RUF001  # lookalike characters on purpose (AI-CORE-22)
"""AI suggestions on ``tags.choose`` reviews in the worker (F-AI-SUGGEST AI-SUGGEST-1 to -8,
-15)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rifferr.ai.base import AIError
from rifferr.ai.tasks.choice_suggest import ChoiceSuggestTask
from rifferr.ai.testing import FakeAIProvider, all_text
from rifferr.core.choices import ChoiceReview
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.review import ReviewStep
from rifferr.steps.tags_choose import TagsChooseParams, TagsChooseStep
from rifferr.worker import ai as worker_ai
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, serving
from tests.worker.test_tags_choose import AFTER, ChooseHarness

RULES = """\
target: genres
rules:
  - label: Alt／Indie
    when: 'subgenres::(?i)indie|alternative'
    actions: [{ set: { genres: [Alt／Indie] } }]
  - label: Punk
    when: 'subgenres::(?i)punk|oi'
    actions: [{ set: { genres: [Punk] } }]
  - label: Electronic
    when: 'subgenres::(?i)house|techno'
    actions: [{ set: { genres: [Electronic] } }]
  - label: +Cross-Genre Collabs
    description: A collaboration whose artists usually come from two or more buckets.
    actions: [{ set: { genres: [+Cross-Genre Collabs] } }]
fallback:
  template: "{{ value }}"
  actions: [{ set: { genres: ["{{ choice }}"] } }]
"""
LOOKALIKES = 'groups:\n  - preferred: "／"\n    variants: ["/", "∕"]\n'
PROFILE_AI = """
ai:
  lookalikes: rules/lookalikes.yaml
  suggest: {mode: %s%s}
  providers:
    - {name: fake, type: fake, model: m1}
"""

STEP = (
    "{id: bucket, uses: tags.choose, with: {options_from: genres.yaml, rank_by: subgenres, "
    "custom: [genres], learn: true%s}}"
)
AI = ", ai: {context: [albumartist, album, year]%s}"


def bucket(ai: str | None = "") -> str:
    return wf(STEP % ("" if ai is None else AI % ai), AFTER)


class AIHarness(ChooseHarness):
    def __init__(self, tmp_path: Path, fake: FakeAIProvider, profile_ai: str) -> None:
        super().__init__(tmp_path)
        path = config_root() / "profiles" / "main" / "profile.yaml"
        path.write_text(path.read_text(encoding="utf-8") + profile_ai, encoding="utf-8")
        self.fake = fake
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), TagsChooseStep()]),
            self.backend,
            self._send,
        )
        self.rules.mkdir(exist_ok=True)
        (self.rules / "genres.yaml").write_text(RULES, encoding="utf-8")
        (self.rules / "lookalikes.yaml").write_text(LOOKALIKES, encoding="utf-8")

    def genre_album(self, *subgenres: str, **fields: Any) -> None:
        self.backend.field_types["subgenres"] = list
        self.backend.field_types["genres"] = list
        self.album_with(
            "G",
            [{"title": "a"}],
            subgenres=list(subgenres),
            genres=[],
            albumartist="GBH",
            year=1983,
            **fields,
        )

    def event_types(self) -> list[str]:
        return [e.type for e in self.db.events_since(RUN, 0)]


def make(mode: str = "unsure", steps: str = "") -> Any:
    @pytest.fixture
    async def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AIHarness]:
        fake = FakeAIProvider()
        monkeypatch.setattr(worker_ai, "load_providers", lambda: ({"fake": fake}, []))
        monkeypatch.setattr(
            worker_ai, "load_tasks", lambda: ({"choice.suggest": ChoiceSuggestTask()}, [])
        )
        harness = AIHarness(tmp_path, fake, PROFILE_AI % (mode, steps))
        async with serving(harness):
            yield harness
        harness.db.close()

    return fixture


h = make()
always = make("always")
overridden = make("always", ", steps: {t/bucket: 'off'}")


def answer(*choices: tuple[str, float]) -> dict[str, Any]:
    return {
        "suggestions": [{"choice": c, "confidence": p, "rationale": f"why {c}"} for c, p in choices]
    }


async def waiting(h: AIHarness, source: str) -> ChoiceReview:
    h.submit(source)
    await h.until(lambda: h.state() == "waiting_review")
    return h.review()


async def test_AI_SUGGEST_7_unsure_asks_when_no_rule_matches(h: AIHarness) -> None:
    h.genre_album()  # no sub-genres: the rules offer nothing
    h.fake.script(answer(("punk", 0.7), ("UK82/Street Punk", 0.4)))
    review = await waiting(h, bucket())
    assert review.ai is not None
    assert review.ai.auto
    got = [(s.label, s.option is not None, s.new) for s in review.ai.suggestions]
    assert got == [("Punk", True, False), ("UK82／Street Punk", False, True)]
    assert "ai.request" in h.event_types()


async def test_AI_SUGGEST_7_unsure_asks_on_a_tie(h: AIHarness) -> None:
    h.genre_album("Indie Rock", "Punk")
    h.fake.script(answer(("Alt/Indie", 0.6)))
    review = await waiting(h, bucket())
    assert [o.matched for o in review.options] == [1, 1]
    assert review.ai is not None
    assert len(h.fake.requests) == 1


async def test_AI_SUGGEST_7_clear_rules_dont_ask(h: AIHarness) -> None:
    h.genre_album("Deep House", "Techno", "Punk")
    review = await waiting(h, bucket())
    assert review.ai is None
    assert review.ai_input is not None  # Ask AI is still offered
    assert h.fake.requests == []


async def test_AI_SUGGEST_7_always_asks(always: AIHarness) -> None:
    always.genre_album("Deep House", "Techno", "Punk")
    always.fake.script(answer(("Electronic", 0.9)))
    review = await waiting(always, bucket())
    assert review.ai is not None
    assert len(always.fake.requests) == 1


async def test_AI_SUGGEST_7_step_mode_and_settings_override(overridden: AIHarness) -> None:
    overridden.genre_album()  # unsure, but Settings say off for t/bucket
    review = await waiting(overridden, bucket(", mode: always"))
    assert review.ai is None
    assert overridden.fake.requests == []


async def test_AI_SUGGEST_7_step_mode_is_the_workflow_default(h: AIHarness) -> None:
    h.genre_album("Deep House", "Techno", "Punk")  # clear: unsure wouldn't ask
    h.fake.script(answer(("Electronic", 0.9)))
    review = await waiting(h, bucket(", mode: always"))
    assert review.ai is not None


async def test_AI_SUGGEST_8_failure_opens_the_review_with_a_note(h: AIHarness) -> None:
    h.genre_album()
    h.fake.script(*[AIError("HTTP 503", retryable=True)] * 3)
    review = await waiting(h, bucket())
    assert review.ai is not None
    assert review.ai.error == "HTTP 503"
    assert review.ai.suggestions == []
    assert h.state() == "waiting_review"  # the lane waits for the person, it didn't fail


async def test_AI_SUGGEST_5_unoffered_rule_becomes_an_option(h: AIHarness) -> None:
    h.genre_album()
    h.fake.script(answer(("+cross-genre collabs", 0.5)))
    review = await waiting(h, bucket())
    assert review.ai is not None
    (s,) = review.ai.suggestions
    offer = review.option(s.option or "")
    assert offer is not None
    assert offer.from_ai
    assert offer.label == "+Cross-Genre Collabs"
    assert (
        offer.description == "A collaboration whose artists usually come from two or more buckets."
    )
    assert offer.actions == [{"set": {"genres": ["+Cross-Genre Collabs"]}}]


async def test_AI_SUGGEST_6_choosing_a_suggested_rule_says_so(h: AIHarness) -> None:
    h.genre_album()
    h.fake.script(answer(("alt∕indie", 0.8)))
    review = await waiting(h, bucket())
    assert review.ai is not None
    (s,) = review.ai.suggestions
    assert s.label == "Alt／Indie"
    await h.supervisor.decide_choice(review.id, "choose", option=s.option, ai_suggestion=1, by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert h.fields("G")["genres"] == ["Alt／Indie"]
    (applied,) = h.changesets.for_run(RUN, "applied")
    assert applied.changeset.rationale == (
        "Alt／Indie (AI suggestion from fake (m1), chosen in review)"
    )


async def test_AI_SUGGEST_6_a_new_value_is_learned_with_preferred_characters(
    h: AIHarness,
) -> None:
    h.genre_album()
    h.fake.script(answer(("Folk/Pop", 0.6)))
    review = await waiting(h, bucket())
    assert review.ai is not None
    (s,) = review.ai.suggestions
    assert (s.new, s.values) == (True, {"genres": ["Folk／Pop"]})
    await h.supervisor.decide_choice(
        review.id, "choose", option="custom", values=s.values, ai_suggestion=1, by="ui"
    )
    await h.until(lambda: h.state() == "succeeded")
    assert h.fields("G")["genres"] == ["Folk／Pop"]
    assert "label: Folk／Pop" in (h.rules / "genres.yaml").read_text(encoding="utf-8")


async def test_AI_SUGGEST_15_a_sure_ai_never_skips_the_review(always: AIHarness) -> None:
    always.genre_album()
    always.fake.script(answer(("Punk", 1.0)))
    review = await waiting(always, bucket())
    assert review.ai is not None
    assert review.ai.suggestions[0].confidence == 1.0
    assert review.state == "pending"
    assert review.suggested is None  # never preselected (AI-SUGGEST-13)
    assert always.state() == "waiting_review"
    assert always.changesets.for_run(RUN, "applied") == []


def test_AI_SUGGEST_15_ai_with_when_single_auto_is_invalid() -> None:
    with pytest.raises(ValidationError, match="ai can't be combined with when_single: auto"):
        TagsChooseParams.model_validate(
            {
                "options": [{"label": "A", "actions": [{"set": {"x": "1"}}]}],
                "when_single": "auto",
                "ai": {},
            }
        )


async def test_AI_SUGGEST_1_no_ai_block_no_ai(h: AIHarness) -> None:
    h.genre_album()
    review = await waiting(h, bucket(None))
    assert review.ai_input is None
    assert review.ai is None
    assert h.fake.requests == []


async def test_AI_SUGGEST_2_request_has_only_the_listed_data(always: AIHarness) -> None:
    h = always
    h.genre_album("Oi", secret_field="do-not-send", path="/music/GBH")
    (h.rules / "notes.md").write_text("UK82 counts as Punk.", encoding="utf-8")
    h.fake.script(answer(("Punk", 0.7)))
    review = await waiting(h, bucket(", guidance: rules/notes.md"))
    sent = all_text(h.fake.requests)
    assert "UK82 counts as Punk." in sent
    assert review.ai_input is not None
    assert "Album: GBH - " in sent
    assert "albumartist: GBH" in sent
    assert "year: 1983" in sent
    assert "subgenres: Oi" in sent
    assert "+Cross-Genre Collabs: A collaboration" in sent  # other labels, with descriptions
    assert '"／" instead of "/"' in sent
    assert "do-not-send" not in sent
    assert "/music/GBH" not in sent


async def test_AI_SUGGEST_3_rule_description_reaches_the_option(h: AIHarness) -> None:
    rules = RULES.replace("  - label: Punk\n", "  - label: Punk\n    description: Fast and loud.\n")
    (h.rules / "genres.yaml").write_text(rules, encoding="utf-8")
    h.genre_album("Oi", "Deep House", "Techno")
    review = await waiting(h, bucket())
    (punk,) = [o for o in review.options if o.label == "Punk"]
    assert punk.description == "Fast and loud."
