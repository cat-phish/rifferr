"""The reference workflow's ``tags.choose`` steps (examples/workflows/foobar-style.yaml,
examples/rules/genres.yaml): what each album-kind option does, and that choosing it twice
changes nothing more (S-TAGS-CHOOSE, AGENTS.md idempotence rule)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from ruamel.yaml import YAML

from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.choice_rules import load_rules
from rifferr.core.facts import ArtistNames
from rifferr.core.selection import Selection
from rifferr.steps.tags_choose import TagsChooseParams, rule_offers
from rifferr.tagscript.evaluator import check_idempotence, run_script
from rifferr.tagscript.models import TagScript
from tests.tagscript.test_reference_scripts import SETTINGS, TYPES

EXAMPLES = Path(__file__).parents[3] / "examples"
LABELS = {
    "label_split": "Split",
    "label_collab": "Collaboration",
    "label_soundtrack": "Soundtrack",
    "label_compilation": "Compilation",
}


def step_params(step_id: str) -> TagsChooseParams:
    doc: Any = YAML(typ="safe").load((EXAMPLES / "workflows" / "foobar-style.yaml").read_text())
    (step,) = [s for s in doc["steps"] if s["id"] == step_id]
    text = YAML(typ="safe")
    del text
    raw = _interpolate(step["with"])
    return TagsChooseParams.model_validate(raw)


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        for name, label in LABELS.items():
            value = value.replace("{{ vars.%s }}" % name, label)  # noqa: UP031
        return value
    if isinstance(value, list):
        return [_interpolate(v) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def backend_for(albumartist: str, **fields: Any) -> FakeBackend:
    backend = FakeBackend.from_fixture(
        {
            "albums": [
                {
                    "album": "X",
                    "albumartist": albumartist,
                    RUN_MARKER: "r",
                    **fields,
                    "items": [{"title": "t", "track": 1}],
                }
            ]
        },
        field_types=TYPES,
    )
    backend.names = ArtistNames(joiners=(" & ", "/", ", "), names=("AC/DC",))
    return backend


def option(label: str) -> TagScript:
    params = step_params("album-kind")
    (found,) = [o for o in params.options if o.label == label]
    return TagScript(idempotence="fail", actions=list(found.actions))


def chosen(backend: FakeBackend, label: str) -> dict[str, Any]:
    script = option(label)
    (album,) = backend.query(Selection())
    result = run_script(script, album, backend)
    assert result.error is None, result.error
    check = check_idempotence(script, result, backend)
    assert check.ok, check.issues
    return {d.field: d.after for d in result.diffs() if d.target.id == album.id}


@pytest.mark.parametrize(
    ("albumartist", "albumartists", "label", "expected"),
    [
        (
            "A & B / C",
            [],
            "Split",
            {
                "albumkind": "split",
                "albumartist": "Split",
                "albumartist_sort": "Split",
                "albumartists": ["A", "B", "C"],
            },
        ),
        (
            "A & B",
            ["Alpha", "Beta"],  # from MusicBrainz: kept
            "Collaboration",
            {
                "albumkind": "collab",
                "albumartist": "Collaboration",
                "albumartist_sort": "Collaboration",
            },
        ),
        (
            "Various Artists",
            [],
            "Compilation",
            {
                "albumkind": "compilation",
                "albumartist": "Compilation",
                "albumartist_sort": "Compilation",
            },
        ),
    ],
)
def test_TAGS_CHOOSE_5_album_kind_options(
    albumartist: str, albumartists: list[str], label: str, expected: dict[str, Any]
) -> None:
    backend = backend_for(albumartist, albumartists=albumartists)
    assert chosen(backend, label) == expected


@SETTINGS
@given(
    st.lists(st.sampled_from(["A", "B", "C D"]), min_size=1, max_size=3),
    st.sampled_from([" & ", " / ", ", "]),
    st.sampled_from(["Split", "Collaboration", "Soundtrack", "Compilation"]),
    st.booleans(),
)
def test_TAGS_CHOOSE_5_album_kind_options_are_idempotent(
    names: list[str], joiner: str, label: str, from_mb: bool
) -> None:
    backend = backend_for(joiner.join(names), albumartists=names if from_mb else [])
    chosen(backend, label)


def test_TAGS_CHOOSE_9_genre_rules_file_parses_and_ranks() -> None:
    rules = load_rules(EXAMPLES / "rules" / "genres.yaml")
    assert rules.target == "genres"
    backend = FakeBackend.from_fixture(
        {
            "albums": [
                {
                    "album": "X",
                    "subgenres": ["Post-Punk", "Shoegaze", "Deep House"],
                    "genres": [],
                    "items": [{"title": "t"}],
                }
            ]
        },
        field_types={**TYPES, "subgenres": list},
    )
    (album,) = backend.query(Selection())
    offers, unmatched = rule_offers(rules, album, backend, "subgenres", set())
    assert [(o.label, o.matched) for o in offers] == [("Rock", 2), ("Electronic", 1)]
    assert unmatched == []


def test_TAGS_CHOOSE_1_reference_steps_validate() -> None:
    for step_id in ("album-kind", "dates-missing", "genre-bucket"):
        step_params(step_id)
