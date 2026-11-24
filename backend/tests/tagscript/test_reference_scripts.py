"""The reference workflow's tag scripts (examples/scripts, docs/original-checklist.md).

Each built-in example script gets the expected changes on a table of albums and a
Hypothesis idempotence property (AGENTS.md testing rules). The same table runs on real
beets in tests/backends/beets/test_reference_scripts.py.
"""

# ruff: noqa: RUF001  # path-characters.yaml swaps in lookalike characters on purpose

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rifferr.backends.base import LibraryBackend
from rifferr.backends.fake import DEFAULT_FIELD_TYPES, FakeBackend
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import check_idempotence, run_script
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.models import TagScript

SCRIPTS = Path(__file__).parents[3] / "examples" / "scripts"
TYPES: dict[str, type] = {
    **DEFAULT_FIELD_TYPES,
    "albumtypes": list,
    "albumartists": list,
    "artists": list,
}


#: release-suffixes.yaml's var defaults, for scripts that use them.
VARS = {"{{ vars.ep_max_tracks }}": "6", "{{ vars.ep_max_seconds }}": "1799"}


def interpolate(text: str) -> str:
    for key, value in VARS.items():
        text = text.replace(key, value)
    return text


def load(name: str) -> TagScript:
    text = (SCRIPTS / name).read_text(encoding="utf-8")
    script, errors = parse_script(text, name, interpolate)
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


#: (album fields, item fields, expected changes {(scope, field): new}).
PATH_CHARACTERS: list[tuple[dict[str, Any], dict[str, Any], dict[tuple[str, str], Any]]] = [
    (
        {"album": "Funk You / Tell Me"},
        {"title": "Funk You / Tell Me"},
        {("album", "album"): "Funk You ／ Tell Me", ("item", "title"): "Funk You ／ Tell Me"},
    ),
    (
        {"album": "Highway", "albumartist": "AC/DC", "albumartists": ["AC/DC"]},
        {"title": "t", "artist": "AC/DC", "artists": ["AC/DC", "Q: B?"]},
        {
            ("album", "albumartist"): "AC／DC",
            ("album", "albumartists"): ["AC／DC"],
            ("item", "artist"): "AC／DC",
            ("item", "artists"): ["AC／DC", "Q꞉ B？"],
        },
    ),
    (
        {"album": "Q: Are We Not Men? A: We Are Devo!"},
        {"title": "Why?"},
        {
            ("album", "album"): "Q꞉ Are We Not Men？ A꞉ We Are Devo!",
            ("item", "title"): "Why？",
        },
    ),
    ({"album": "Plain"}, {"title": "Plain"}, {}),
    ({"album": "Funk You ／ Tell Me"}, {"title": "Why？"}, {}),  # already replaced
]


#: dates.yaml: (album date fields, expected changes). Absent dates are 0, as beets stores
#: them; releasedate as beets renders it (zero-padded month and day).
NO_DATES = {
    f: 0 for f in ("year", "month", "day", "original_year", "original_month", "original_day")
}
DATES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (
        {
            "year": 2004,
            "month": 3,
            "day": 22,
            "original_year": 1999,
            "original_month": 5,
            "original_day": 1,
        },
        {"releasedate": "2004-03-22", "year": 1999, "month": 5, "day": 1},
    ),
    (
        {"year": 2004, "month": 3, "day": 22},
        {
            "releasedate": "2004-03-22",
            "original_year": 2004,
            "original_month": 3,
            "original_day": 22,
        },
    ),
    (
        {"year": 2004, "month": 3},
        {"releasedate": "2004-03", "original_year": 2004, "original_month": 3},
    ),
    ({"year": 2004}, {"releasedate": "2004", "original_year": 2004}),
    ({"original_year": 1999}, {}),  # no release date to keep: left for a person
    ({}, {}),
    (  # already done: nothing changes
        {"year": 1999, "original_year": 1999, "releasedate": "2004-03-22"},
        {},
    ),
]


#: likely-ep.yaml: (album fields, tracks, seconds per track, expected changes).
LIKELY_EP: list[tuple[dict[str, Any], int, float, dict[str, Any]]] = [
    ({"albumtype": "album"}, 5, 240.0, {"albumtype": "ep"}),
    ({"albumtype": "album"}, 6, 299.0, {"albumtype": "ep"}),  # 29:54
    ({"albumtype": "album"}, 6, 300.0, {}),  # exactly 30 minutes
    ({"albumtype": "album"}, 7, 120.0, {}),
    ({"albumtype": "ep"}, 4, 240.0, {}),
    ({"albumtype": "single"}, 2, 200.0, {}),
    ({}, 3, 200.0, {"albumtype": "ep"}),
]


def all_changes(backend: LibraryBackend, script: TagScript) -> dict[tuple[str, str], Any]:
    (album,) = backend.query(Selection())
    result = run_script(script, album, backend)
    assert result.error is None, result.error
    assert check_idempotence(script, result, backend).ok
    return {(d.target.scope, d.field): d.after for d in result.diffs()}


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


@pytest.mark.parametrize(("album", "item", "expected"), PATH_CHARACTERS)
def test_TAGSCRIPT_8_path_characters_script(
    album: dict[str, Any], item: dict[str, Any], expected: dict[tuple[str, str], Any]
) -> None:
    backend = FakeBackend.from_fixture(
        {"albums": [{**album, "items": [{"track": 1, **item}]}]}, field_types=TYPES
    )
    assert all_changes(backend, load("path-characters.yaml")) == expected


@pytest.mark.parametrize(("fields", "expected"), DATES)
def test_TAGSCRIPT_7_dates_script(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    got = changes(fake({"album": "A", **NO_DATES, **fields}), load("dates.yaml"))
    # FakeBackend renders $month and $day unpadded; beets pads them (see the beets test).
    unpadded = {
        k: re.sub(r"-0(\d)", r"-\1", v) if k == "releasedate" else v for k, v in expected.items()
    }
    assert got == unpadded


def tracks(n: int, seconds: float) -> list[dict[str, Any]]:
    return [{"title": f"t{i}", "track": i, "length": seconds} for i in range(1, n + 1)]


@pytest.mark.parametrize(("fields", "n", "seconds", "expected"), LIKELY_EP)
def test_ALBUM_FACTS_1_likely_ep_script(
    fields: dict[str, Any], n: int, seconds: float, expected: dict[str, Any]
) -> None:
    backend = FakeBackend.from_fixture(
        {"albums": [{"album": "A", **fields, "items": tracks(n, seconds)}]}, field_types=TYPES
    )
    assert changes(backend, load("likely-ep.yaml")) == expected


# -- idempotence (TAGSCRIPT-19) -----------------------------------------------------------

words = st.text(st.characters(categories=("L", "N"), max_codepoint=0x24F), min_size=1, max_size=8)
endings = st.sampled_from(
    ["", " EP", " ep", " (EP)", " [Ep]", " Single", " (single)", " (Compilation)", " (Anthology)"]
)
#: Word separators, including the characters path-characters.yaml replaces.
separators = st.sampled_from([" ", " / ", "/", ": ", "?", " ／ ", "？"])
names = st.builds(
    lambda first, rest, end: first + "".join(sep + w for sep, w in rest) + end,
    words,
    st.lists(st.tuples(separators, words), max_size=2),
    endings,
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


dates = st.fixed_dictionaries(
    {
        "year": st.sampled_from([0, 1999, 2004]),
        "month": st.integers(0, 12),
        "day": st.integers(0, 28),
        "original_year": st.sampled_from([0, 1999, 2004]),
        "original_month": st.integers(0, 12),
        "original_day": st.integers(0, 28),
    },
    optional={"releasedate": st.sampled_from(["2004", "2004-03-22"])},
)


@SETTINGS
@given(dates)
def test_TAGSCRIPT_19_dates_script_is_idempotent(fields: dict[str, Any]) -> None:
    backend = fake({"album": "A", **fields})
    script = load("dates.yaml")
    (album,) = backend.query(Selection())
    first = run_script(script, album, backend)
    assert first.error is None
    check = check_idempotence(script, first, backend)
    assert check.ok, (fields, check.issues)


@SETTINGS
@given(
    albums,
    st.sampled_from(
        ["release-types.yaml", "compilation.yaml", "path-characters.yaml", "likely-ep.yaml"]
    ),
)
def test_TAGSCRIPT_19_reference_scripts_are_idempotent(fields: dict[str, Any], name: str) -> None:
    backend = fake(fields)
    script = load(name)
    (album,) = backend.query(Selection())
    first = run_script(script, album, backend)
    assert first.error is None
    check = check_idempotence(script, first, backend)
    assert check.ok, (fields, check.issues)


#: subgenres.yaml: (album fields, expected album-level changes).
SUBGENRES: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({"genres": ["Post-Rock", "Math Rock"]}, {"subgenres": ["Post-Rock", "Math Rock"]}),
    ({"genres": ["Post-Rock"], "subgenres": ["Shoegaze"]}, {}),  # once only
    ({"genres": []}, {}),
]


@pytest.mark.parametrize(("fields", "expected"), SUBGENRES)
def test_TAGSCRIPT_5_subgenres_script(fields: dict[str, Any], expected: dict[str, Any]) -> None:
    backend = fake({"album": "A", **fields})
    backend.field_types["subgenres"] = list
    assert changes(backend, load("subgenres.yaml")) == expected


@SETTINGS
@given(
    st.fixed_dictionaries(
        {},
        optional={
            "genres": st.lists(st.sampled_from(["Rock", "Post-Rock", "Jazz"]), max_size=3),
            "subgenres": st.lists(st.sampled_from(["Shoegaze", "Rock"]), max_size=2),
        },
    )
)
def test_TAGSCRIPT_19_subgenres_script_is_idempotent(fields: dict[str, Any]) -> None:
    backend = fake({"album": "A", **fields})
    backend.field_types["subgenres"] = list
    script = load("subgenres.yaml")
    (album,) = backend.query(Selection())
    first = run_script(script, album, backend)
    assert first.error is None
    check = check_idempotence(script, first, backend)
    assert check.ok, (fields, check.issues)


#: kind-album-name.yaml: (album fields, expected album-level changes).
KIND_ALBUM_NAME: list[tuple[dict[str, Any], dict[str, Any]]] = [
    (
        {"album": "Split EP", "albumkind": "split", "albumartists": ["A", "B"]},
        {"album": "A / B - Split EP"},
    ),
    ({"album": "A / B - Split EP", "albumkind": "split", "albumartists": ["A", "B"]}, {}),
    (
        {"album": "Together, Again", "albumkind": "collab", "albumartists": ["A, Jr.", "B"]},
        {"album": "A, Jr. / B - Together, Again"},
    ),
    ({"album": "Hits", "albumkind": "compilation", "albumartists": ["A", "B"]}, {}),
    ({"album": "Live", "albumartists": ["A"]}, {}),
]


@pytest.mark.parametrize(("fields", "expected"), KIND_ALBUM_NAME)
def test_TEMPLATE_CONDITIONS_8_kind_album_name_script(
    fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert changes(fake(fields), load("kind-album-name.yaml")) == expected


@SETTINGS
@given(
    st.fixed_dictionaries(
        {"album": names},
        optional={
            "albumkind": st.sampled_from(["split", "collab", "compilation"]),
            "albumartists": st.lists(st.sampled_from(["A", "B", "C, D", "E / F"]), max_size=3),
        },
    )
)
def test_TEMPLATE_CONDITIONS_8_kind_album_name_is_idempotent(fields: dict[str, Any]) -> None:
    backend = fake(fields)
    script = load("kind-album-name.yaml")
    (album,) = backend.query(Selection())
    first = run_script(script, album, backend)
    assert first.error is None
    check = check_idempotence(script, first, backend)
    assert check.ok, (fields, check.issues)
