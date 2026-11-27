"""The ``tag`` AI task on its own (S-AI-TAG AI-TAG-5, -7)."""

from __future__ import annotations

import pytest

from rifferr.ai.base import TaskContext
from rifferr.ai.tasks.tag import TagAnswer, TagInput, TagTarget, TagTask

TASK = TagTask()


def inp(**kw: object) -> TagInput:
    base: dict[str, object] = {
        "album": "Joy Division - Unknown Pleasures",
        "album_context": {"year": 1979},
        "fields": ["genre"],
        "targets": [TagTarget(id="album", label="Unknown Pleasures", fields={"genre": None})],
        "vocabulary": {"genre": "rules/genres.txt"},
    }
    return TagInput.model_validate(base | kw)


CTX = TaskContext(files={"vocabulary:genre": "Rock\nPost-Punk\n"})


def test_AI_TAG_5_files_and_sends() -> None:
    i = inp(rules="rules/notes.md", denylist={"genre": "rules/deny.txt"})
    assert TASK.files(i) == {
        "rules": "rules/notes.md",
        "vocabulary:genre": "rules/genres.txt",
        "denylist:genre": "rules/deny.txt",
    }
    assert TASK.sends(i) == [
        "album label",
        "field genre (current values)",
        "field year",
        "rules file rules/notes.md",
        "vocabulary file rules/genres.txt",
    ]


def test_AI_TAG_5_prompt_lists_fields_vocabulary_and_current_values() -> None:
    _, user = TASK.build(inp(multi=["genre"]), CTX)
    assert "- genre: several values (answer a list); allowed values: Rock, Post-Punk" in user
    assert "[album] Unknown Pleasures\n  current genre: (empty)" in user
    assert "- year: 1979" in user


def test_AI_TAG_7_unknown_targets_are_ignored() -> None:
    answer = TagAnswer.model_validate(
        {
            "targets": [
                {
                    "id": "album",
                    "fields": [{"field": "genre", "value": "post-PUNK", "confidence": 1}],
                },
                {"id": "item-9", "fields": []},
            ]
        }
    )
    out = TASK.validate(answer, inp(), CTX)
    assert [(p.id, p.values[0].value) for p in out.value] == [("album", "Post-Punk")]
    assert [(r.value, r.reason) for r in out.rejected] == [("item-9", "unknown_target")]


def test_AI_TAG_6_duplicate_vocabulary_entries_refuse_to_build() -> None:
    ctx = TaskContext(files={"vocabulary:genre": "Rock\nrock\n"})
    with pytest.raises(ValueError, match="same after folding"):
        TASK.build(inp(), ctx)
