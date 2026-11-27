# ruff: noqa: RUF001  # lookalike characters on purpose (AI-CORE-22)
"""The checks of an AI field answer that need no backend (S-AI-TAG AI-TAG-6, -7)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rifferr.core.ai_validate import (
    AnsweredValue,
    LookalikeGroup,
    Lookalikes,
    Vocabulary,
    check_field_values,
    parse_value_list,
)

SLASH = Lookalikes(groups=[LookalikeGroup(preferred="／", variants=["/"])])


def test_AI_TAG_6_list_files_skip_blank_lines_and_comments() -> None:
    assert parse_value_list("# genres\nRock\n\n  Post-Punk  \n#Jazz\n") == ["Rock", "Post-Punk"]


def test_AI_TAG_7_checks_run_in_order() -> None:
    vocab = {"genre": Vocabulary(["Rock", "Alt／Indie"], SLASH)}
    deny = {"genre": Vocabulary(["Rock"], SLASH)}
    kept, rejected = check_field_values(
        [
            AnsweredValue(field="mood", value="x", confidence=1),
            AnsweredValue(field="genre", value=["rock", "alt/indie", "Polka"], confidence=0.9),
            AnsweredValue(field="genre", value="Jazz", confidence=1),
            AnsweredValue(field="style", value="y", confidence=0.1),
        ],
        fields=["genre", "style"],
        vocabularies=vocab,
        denylists=deny,
        min_confidence=0.5,
        lookalikes=SLASH,
    )
    assert [(k.field, k.value) for k in kept] == [("genre", ["Alt／Indie"])]
    assert [(r.value, r.reason) for r in rejected] == [
        ("x", "field_not_allowed"),
        ("rock", "denylisted"),
        ("Polka", "not_in_vocabulary"),
        ("Jazz", "duplicate_field"),
        ("y", "low_confidence"),
    ]


def test_AI_TAG_7_flag_keeps_with_a_warning_and_no_vocabulary_uses_lookalikes() -> None:
    kept, _ = check_field_values(
        [
            AnsweredValue(field="genre", value="Darkwave", confidence=0.5),
            AnsweredValue(field="title", value="A/B  Side", confidence=0.5),
        ],
        fields=["genre", "title"],
        vocabularies={"genre": Vocabulary(["Rock"])},
        on_unknown="flag",
        lookalikes=SLASH,
    )
    assert [(k.value, k.warnings) for k in kept] == [
        ("Darkwave", ["not_in_vocabulary"]),
        ("A／B Side", []),
    ]


def test_AI_TAG_7_a_field_with_every_element_dropped_is_dropped() -> None:
    kept, rejected = check_field_values(
        [AnsweredValue(field="genre", value=["", " "], confidence=1)], fields=["genre"]
    )
    assert kept == []
    assert [r.reason for r in rejected] == ["empty", "empty"]


names = st.sampled_from(["genre", "mood", "style", "year"])
values = st.text(alphabet="ab /／ROCK", max_size=8)
answers = st.lists(
    st.builds(
        AnsweredValue,
        field=names,
        value=st.one_of(values, st.lists(values, max_size=3)),
        confidence=st.floats(0, 1),
    ),
    max_size=6,
)


@given(answers)
def test_AI_TAG_7_only_allowlisted_fields_survive_and_checking_is_idempotent(
    given_answers: list[AnsweredValue],
) -> None:
    opts = {
        "fields": ["genre", "mood"],
        "vocabularies": {"genre": Vocabulary(["Rock", "a／b"], SLASH)},
        "denylists": {"mood": Vocabulary(["ab"], SLASH)},
        "min_confidence": 0.3,
        "lookalikes": SLASH,
    }
    kept, _ = check_field_values(given_answers, **opts)  # pyright: ignore[reportArgumentType]
    assert {k.field for k in kept} <= {"genre", "mood"}
    assert len({k.field for k in kept}) == len(kept)
    again, rejected = check_field_values(
        [AnsweredValue(field=k.field, value=k.value, confidence=k.confidence) for k in kept],
        **opts,  # pyright: ignore[reportArgumentType]
    )
    assert again == kept
    assert rejected == []
