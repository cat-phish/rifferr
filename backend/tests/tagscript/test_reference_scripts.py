"""The reference workflow's tag scripts (examples/scripts, docs/original-checklist.md).

Each built-in example script gets the expected changes on a table of albums and a
Hypothesis idempotence property (AGENTS.md testing rules). The same table runs on real
beets in tests/backends/beets/test_reference_scripts.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rifferr.backends.base import LibraryBackend
from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import check_idempotence, run_script
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.models import TagScript

SCRIPTS = Path(__file__).parents[3] / "examples" / "scripts"
TYPES: dict[str, type] = {"albumtypes": list}


def load(name: str) -> TagScript:
    script, errors = parse_script((SCRIPTS / name).read_text(encoding="utf-8"), name, str)
    assert script is not None, errors
    return script


#: (album fields, expected album-level changes {field: new}). Album and type as beets
#: stores them; albumtypes is the MusicBrainz secondary types list.
RELEASE_TYPES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({"album": "Live", "albumtype": "ep"}, {"album": "Live (EP)"}),
    ({"album": "Live", "albumtype": "single"}, {"album": "Live (Single)"}),
    ({"album": "Live", "albumtype": "album"}, {}),
    ({"album": "Live (EP)", "albumtype": "ep"}, {}),  # never "EP (EP)"
    ({"album": "Live EP", "albumtype": "ep"}, {}),
    ({"album": "Live [ep]", "albumtype": "ep"}, {}),
    ({"album": "Live Ep", "albumtype": "album"}, {"albumtype": "ep"}),
    ({"album": "Live (EP)", "albumtype": "album"}, {"albumtype": "ep"}),
    ({"album": "Hits Single", "albumtype": "album"}, {"albumtype": "single"}),
    ({"album": "Live", "albumtype": "single", "comments": "x"}, {"album": "Live (Single)"}),
    ({"album": "Single Mothers", "albumtype": "album"}, {}),  # only the end counts
    ({"album": "Deep", "albumtype": "album"}, {}),  # "ep" inside a word
    ({"album": "Steep", "albumtype": "ep"}, {"album": "Steep (EP)"}),
    ({"album": "Live"}, {}),  # no albumtype
]

COMPILATIONS: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (
        {"album": "Best Of", "albumtypes": ["album", "compilation"]},
        {"album": "Best Of (Compilation)"},
    ),
    ({"album": "Best Of (Compilation)", "albumtypes": ["album", "compilation"]}, {}),
    ({"album": "Best Of (Anthology)", "albumtypes": ["album", "compilation"]}, {}),
    ({"album": "Best Of", "albumtypes": ["album", "live"]}, {}),
    ({"album": "Best Of"}, {}),
]


def changes(backend: LibraryBackend, script: TagScript) -> dict[str, Any]:
    (album,) = backend.query(Selection())
    result = run_script(script, album, backend)
    assert result.error is None, result.error
    assert check_idempotence(script, result, backend).ok
    return {d.field: d.after for d in result.diffs() if d.target.id == album.id}


def fake(fields: dict[str, Any]) -> FakeBackend:
    return FakeBackend.from_fixture(
        {"albums": [{**fields, "items": [{"title": "t", "track": 1}]}]}, field_types=TYPES
    )


@pytest.mark.parametrize(("fields", "expected"), RELEASE_TYPES)
def test_TAGSCRIPT_3_release_types_script(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    assert changes(fake(fields), load("release-types.yaml")) == expected


@pytest.mark.parametrize(("fields", "expected"), COMPILATIONS)
def test_TAGSCRIPT_3_compilation_script(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    assert changes(fake(fields), load("compilation.yaml")) == expected


# -- idempotence (TAGSCRIPT-19) -----------------------------------------------------------

words = st.text(st.characters(categories=("L", "N"), max_codepoint=0x24F), min_size=1, max_size=8)
endings = st.sampled_from(
    ["", " EP", " ep", " (EP)", " [Ep]", " Single", " (single)", " (Compilation)", " (Anthology)"]
)
names = st.builds(
    lambda ws, end: " ".join(ws) + end, st.lists(words, min_size=1, max_size=3), endings
)
albums = st.fixed_dictionaries(
    {"album": names},
    optional={
        "albumtype": st.sampled_from(["album", "ep", "single", "compilation", "other"]),
        "albumtypes": st.lists(
            st.sampled_from(["album", "ep", "single", "compilation", "live"]), max_size=3
        ),
    },
)
SETTINGS = settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(albums, st.sampled_from(["release-types.yaml", "compilation.yaml"]))
def test_TAGSCRIPT_19_reference_scripts_are_idempotent(fields: dict[str, Any], name: str) -> None:
    backend = fake(fields)
    script = load(name)
    (album,) = backend.query(Selection())
    first = run_script(script, album, backend)
    assert first.error is None
    check = check_idempotence(script, first, backend)
    assert check.ok, (fields, check.issues)
