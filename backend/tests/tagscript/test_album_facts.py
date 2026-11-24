"""C-ALBUM-FACTS on FakeBackend: computed album facts in tag-script conditions."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rifferr.backends.fake import DEFAULT_FIELD_TYPES, FakeBackend
from rifferr.core.facts import ArtistNames, album_facts, multi_artist
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import run_script
from rifferr.tagscript.loader import parse_script

TYPES: dict[str, type] = {**DEFAULT_FIELD_TYPES, "albumartists": list, "artists": list}
NAMES = ArtistNames(joiners=(" & ", " / ", " x ", ", "), names=("AC/DC", "Simon & Garfunkel"))


def backend(album: dict[str, Any], items: list[dict[str, Any]]) -> FakeBackend:
    fb = FakeBackend.from_fixture({"albums": [{**album, "items": items}]}, field_types=TYPES)
    fb.names = NAMES
    return fb


def run(fb: FakeBackend, text: str) -> dict[str, Any]:
    script, errors = parse_script(text, "t.yaml", str)
    assert script is not None, errors
    (album,) = fb.query(Selection())
    result = run_script(script, album, fb)
    assert result.error is None, result.error
    return {d.field: d.after for d in result.diffs() if d.target.id == album.id}


def test_ALBUM_FACTS_1_ALBUM_FACTS_6_built_in_facts_on_fake() -> None:
    fb = backend(
        {"album": "A", "albumartist": "X", "albumartists": ["X", "Y", "Z"]},
        [{"title": f"t{n}", "track": n, "length": 100.6} for n in range(1, 4)],
    )
    (album,) = fb.query(Selection())
    facts = fb.album_facts(album)
    assert facts["rf_items"] == 3
    assert facts["rf_length"] == 301
    assert facts["rf_count_albumartists"] == 3


def test_ALBUM_FACTS_1_facts_work_in_when_queries() -> None:
    """B-044: at most 6 tracks and under 30 minutes."""
    script = """
actions:
  - when: "rf_items:..6 rf_length:..1799 ^albumtype:ep"
    set: { comments: likely EP }
"""
    short = backend({"album": "A"}, [{"title": "t", "track": 1, "length": 240.0}] * 5)
    long = backend({"album": "A"}, [{"title": "t", "track": 1, "length": 600.0}] * 5)
    assert run(short, script) == {"comments": "likely EP"}
    assert run(long, script) == {}


def test_ALBUM_FACTS_2_named_counts_and_shares() -> None:
    fb = backend({"album": "A"}, [{"title": "x (Live)", "track": 1}, {"title": "y", "track": 2}])
    fb.fact_queries = {"live_tracks": "title::Live"}
    (album,) = fb.query(Selection())
    facts = fb.album_facts(album)
    assert facts["rf_live_tracks"] == 1
    assert facts["rf_share_live_tracks"] == 50


def test_ALBUM_FACTS_3_facts_see_earlier_actions() -> None:
    fb = backend({"album": "A", "albumartists": ["X"]}, [{"title": "t", "track": 1}])
    out = run(
        fb,
        """
actions:
  - set: { albumartists: [X, Y] }
  - when: "rf_count_albumartists:2"
    set: { comments: two }
""",
    )
    assert out["comments"] == "two"


def test_ALBUM_FACTS_5_facts_cannot_be_set() -> None:
    fb = backend({"album": "A"}, [{"title": "t", "track": 1}])
    script, _ = parse_script("actions:\n  - set: { rf_items: 3 }\n", "t.yaml", str)
    assert script is not None
    (album,) = fb.query(Selection())
    result = run_script(script, album, fb)
    assert result.error is not None
    assert "computed album fact" in result.error.message


@pytest.mark.parametrize(
    ("text", "joined"),
    [
        ("A & B", True),
        ("A x B", True),
        ("AC/DC", False),
        ("Simon & Garfunkel", False),
        ("simon & garfunkel", False),
        ("Simon & Garfunkel & Friends", True),
        ("Boxer", False),  # " x " needs spaces
        ("Plain", False),
    ],
)
def test_ALBUM_FACTS_8_joined_album_artist(text: str, joined: bool) -> None:
    assert NAMES.has_joiner(text) is joined
    facts = album_facts({"albumartist": text}, [], NAMES)
    assert facts["rf_joined_albumartist"] == int(joined)


def test_ALBUM_FACTS_7_no_rules_means_no_joiners() -> None:
    assert ArtistNames().has_joiner("A & B") is False


def test_ALBUM_FACTS_7_split_keeps_known_names() -> None:
    assert NAMES.split("AC/DC / Simon & Garfunkel & Low") == ["AC/DC", "Simon & Garfunkel", "Low"]
    assert NAMES.split("Plain") == ["Plain"]


@pytest.mark.parametrize(
    ("item", "multi"),
    [
        # MusicBrainz credits: both still in the artist text
        ({"artist": "A & B", "artists": ["A", "B"]}, True),
        # ftintitle moved the featured artist into the title
        ({"artist": "A", "artists": ["A", "B"], "title": "Song (feat. B)"}, False),
        # no credits: joiners
        ({"artist": "A & B"}, True),
        ({"artist": "AC/DC"}, False),
        ({"artist": "A", "artists": ["A"]}, False),
    ],
)
def test_ALBUM_FACTS_9_multi_artist_tracks(item: dict[str, Any], multi: bool) -> None:
    assert multi_artist(item, NAMES) is multi


def test_ALBUM_FACTS_9_ALBUM_FACTS_10_share_rounds_down() -> None:
    items = [{"artist": "A & B"}, {"artist": "A & B"}, {"artist": "A"}]
    facts = album_facts({}, items, NAMES)
    assert facts["rf_multi_artist_tracks"] == 2
    assert facts["rf_share_multi_artist_tracks"] == 66
    assert album_facts({}, [], NAMES)["rf_share_multi_artist_tracks"] == 0


@given(st.text(max_size=30))
def test_ALBUM_FACTS_7_split_never_loses_text(text: str) -> None:
    parts = NAMES.split(text)
    assert all(p.strip() == p and p for p in parts)
    if not NAMES.has_joiner(text) and text.strip():
        assert parts == [text.strip()]
