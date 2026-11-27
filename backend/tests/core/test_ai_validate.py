# ruff: noqa: RUF001  # lookalike characters on purpose (AI-CORE-13)
"""Vocabulary matching and lookalikes (AI-CORE-13, -22)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from rifferr.core.ai_validate import (
    LookalikeGroup,
    Lookalikes,
    Vocabulary,
    VocabularyError,
    match_key,
    match_vocabulary,
)

# The author's lookalikes (BACKLOG B-093): user data, here only as test input.
LOOK = Lookalikes(
    groups=[
        LookalikeGroup(preferred="／", variants=["/", "∕", "⁄", "⧸", "╱", "⟋"]),
        LookalikeGroup(preferred="꞉", variants=[":", "∶", "ː", "˸", "﹕", "：", "⁚", "፡", "⦂"]),
        LookalikeGroup(preferred="？", variants=["?", "﹖", "ʔ"]),
    ]
)
BUCKETS = ["Alt／Indie", "Garage Rock／Punk", "Hip-Hop", "Ska (2nd Wave／2-Tone)", "R&B"]


def test_AI_CORE_13_known_values_take_the_vocabulary_spelling() -> None:
    vocab = Vocabulary(BUCKETS, LOOK)
    got, rejected = match_vocabulary(
        ["Alt/Indie", "garage rock∕punk", "SKA (2nd wave/2-tone)", "r&b"], vocab, "strict"
    )
    assert [m.value for m in got] == [*BUCKETS[:2], "Ska (2nd Wave／2-Tone)", "R&B"]
    assert all(m.known for m in got)
    assert rejected == []


def test_AI_CORE_13_strict_drops_unknown() -> None:
    got, rejected = match_vocabulary(["Folk/Pop", "Alt/Indie"], Vocabulary(BUCKETS, LOOK), "strict")
    assert [m.value for m in got] == ["Alt／Indie"]
    assert [(r.value, r.reason) for r in rejected] == [("Folk/Pop", "not_in_vocabulary")]


def test_AI_CORE_13_open_marks_new_with_preferred_characters() -> None:
    got, _ = match_vocabulary(["Folk/Pop", "UK82: Street?"], Vocabulary(BUCKETS, LOOK), "open")
    assert [(m.value, m.new, m.known) for m in got] == [
        ("Folk／Pop", True, False),
        ("UK82꞉ Street？", True, False),
    ]


def test_AI_CORE_13_flag_keeps_with_warning() -> None:
    got, _ = match_vocabulary(["Folk/Pop"], Vocabulary(BUCKETS, LOOK), "flag")
    assert got[0].value == "Folk／Pop"
    assert got[0].warnings == ["not_in_vocabulary"]
    assert not got[0].new


def test_AI_CORE_13_no_spelling_guesses() -> None:
    vocab = Vocabulary(BUCKETS, LOOK)
    assert vocab.get("hip hop") is None  # a hyphen isn't a lookalike of a space
    assert vocab.get("Alt / Indie") is None  # spaces are collapsed, not removed
    assert vocab.get("  alt/INDIE ") == "Alt／Indie"


def test_AI_CORE_13_without_lookalikes_only_nfkc() -> None:
    vocab = Vocabulary(BUCKETS)
    assert vocab.get("Alt/Indie") == "Alt／Indie"  # NFKC folds U+FF0F
    assert vocab.get("Alt∕Indie") is None  # U+2215 has no NFKC mapping
    got, _ = match_vocabulary(["Folk/Pop"], vocab, "open")
    assert got[0].value == "Folk/Pop"


def test_AI_CORE_13_duplicates_after_matching_keep_first() -> None:
    got, _ = match_vocabulary(
        ["alt/indie", "Alt／Indie", "Folk/Pop", "folk／pop"], Vocabulary(BUCKETS, LOOK), "open"
    )
    assert [m.value for m in got] == ["Alt／Indie", "Folk／Pop"]


def test_AI_CORE_13_clashing_vocabulary_entries_fail() -> None:
    with pytest.raises(VocabularyError, match="'Alt/Indie' and 'Alt／Indie'"):
        Vocabulary(["Alt/Indie", "Alt／Indie"], LOOK)
    Vocabulary(["Rock", "Rock"], LOOK)  # the same entry twice is harmless


def test_AI_CORE_13_empty_values_are_rejected() -> None:
    got, rejected = match_vocabulary(["  "], Vocabulary(BUCKETS, LOOK), "open")
    assert got == []
    assert rejected[0].reason == "empty"


def test_AI_CORE_22_lookalike_file_rules() -> None:
    with pytest.raises(ValidationError, match="listed in two groups"):
        Lookalikes.model_validate(
            {
                "groups": [
                    {"preferred": "／", "variants": ["/"]},
                    {"preferred": "|", "variants": ["/"]},
                ]
            }
        )
    with pytest.raises(ValidationError, match="both preferred and a variant"):
        Lookalikes.model_validate({"groups": [{"preferred": "/", "variants": ["/"]}]})
    with pytest.raises(ValidationError, match="single characters"):
        Lookalikes.model_validate({"groups": [{"preferred": "／", "variants": ["//"]}]})


def test_AI_CORE_22_prompt_line_names_preferred_characters() -> None:
    line = LOOK.prompt_line() or ""
    assert '"／" instead of "/"' in line
    assert '"꞉" instead of ":"' in line
    assert Lookalikes().prompt_line() is None


_chars = st.text(
    alphabet=st.sampled_from(list("aZ /∕／:꞉：?？ʔ-&()ß")) | st.characters(), max_size=30
)


@given(_chars)
def test_AI_CORE_13_match_key_is_idempotent(text: str) -> None:
    once = match_key(text, LOOK)
    assert match_key(once, LOOK) == once


@given(_chars)
def test_AI_CORE_13_rewrite_is_idempotent(text: str) -> None:
    once = LOOK.rewrite(text)
    assert LOOK.rewrite(once) == once
    assert match_key(once, LOOK) == match_key(text, LOOK)


@given(st.lists(_chars, max_size=8))
def test_AI_CORE_13_matching_is_idempotent(values: list[str]) -> None:
    vocab = Vocabulary(BUCKETS, LOOK)
    first, _ = match_vocabulary(values, vocab, "open")
    again, rejected = match_vocabulary([m.value for m in first], vocab, "open")
    assert [m.value for m in again] == [m.value for m in first]
    assert rejected == []
