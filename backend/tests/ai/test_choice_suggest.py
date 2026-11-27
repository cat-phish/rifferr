"""The ``choice.suggest`` task on its own (AI-SUGGEST-2, -4, -5)."""

from __future__ import annotations

from rifferr.ai.base import TaskContext
from rifferr.ai.tasks.choice_suggest import ChoiceSuggestTask, SuggestAnswer
from rifferr.core.choices import ChoiceSuggestInput, SuggestOption, SuggestRule

TASK = ChoiceSuggestTask()


def inp(**kw: object) -> ChoiceSuggestInput:
    base: dict[str, object] = {
        "album": "A - B",
        "options": [SuggestOption(key="r0", label="Punk", rule=True, matched=2)],
        "rules": [SuggestRule(label="Punk"), SuggestRule(label="Metal")],
        "custom": ["genres"],
    }
    return ChoiceSuggestInput.model_validate(base | kw)


def validate(
    i: ChoiceSuggestInput, *choices: str
) -> list[tuple[str, str | None, str | None, bool]]:
    answer = SuggestAnswer.model_validate(
        {"suggestions": [{"choice": c, "confidence": 0.5} for c in choices]}
    )
    out = TASK.validate(answer, i, TaskContext())
    return [(s.label, s.option, s.rule, s.new) for s in out.value]


def test_AI_SUGGEST_5_known_offered_rule_and_new() -> None:
    assert validate(inp(), "punk", "METAL", "Crust") == [
        ("Punk", "r0", None, False),
        ("Metal", None, "Metal", False),
        ("Crust", None, None, True),
    ]


def test_AI_SUGGEST_5_no_new_without_allow_new_or_custom() -> None:
    assert validate(inp(allow_new=False), "Crust", "Punk") == [("Punk", "r0", None, False)]
    assert validate(inp(custom=[]), "Crust") == []


def test_AI_SUGGEST_4_ranked_merged_and_capped() -> None:
    got = validate(inp(max_suggestions=2), "Punk", "punk", "Metal", "Crust")
    assert [g[0] for g in got] == ["Punk", "Metal"]


def test_AI_SUGGEST_2_prompt_names_new_values_only_when_allowed() -> None:
    system, user = TASK.build(inp(), TaskContext())
    assert "may suggest a new value" in system
    assert "Options the rules offer for this album:\n- Punk" in user
    assert "Other known labels:\n- Metal" in user
    system, _ = TASK.build(inp(allow_new=False), TaskContext())
    assert "Suggest only labels from the lists." in system
