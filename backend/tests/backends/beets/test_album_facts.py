"""C-ALBUM-FACTS on beets: typed facts in beets queries, config and artist-name rules."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rifferr.backends.beets import config
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import run_script
from rifferr.tagscript.loader import parse_script
from tests.backends.beets.conftest import BeetsEnv, seed

CONFIG = """
rifferr:
  artist_names: artist-names.yaml
  album_facts:
    live_tracks: "title::(?i)[(]live[)]"
"""
RULES = 'joiners: [" & ", " / "]\nnames: [AC/DC]\n'


def changes(env: BeetsEnv, text: str, albumartist: str = "A & B", n: int = 5) -> dict[str, object]:
    (env.beetsdir / "artist-names.yaml").write_text(RULES)
    backend = env.backend(CONFIG)
    items = [{"title": f"t{i} (Live)", "track": i, "length": 200.0} for i in range(1, n + 1)]
    seed(backend, [{"albumartist": albumartist, "album": "X", "items": items}])
    script, errors = parse_script(text, "t.yaml", str)
    assert script is not None, errors
    (album,) = backend.query(Selection())
    result = run_script(script, album, backend)
    assert result.error is None, result.error
    return {d.field: d.after for d in result.diffs() if d.target.id == album.id}


def test_ALBUM_FACTS_1_numeric_range_queries(beets_env: BeetsEnv) -> None:
    script = 'actions:\n  - when: "rf_items:..6 rf_length:..1799"\n    set: { comments: EP }\n'
    assert changes(beets_env, script) == {"comments": "EP"}


def test_ALBUM_FACTS_1_too_long_is_not_matched(beets_env: BeetsEnv) -> None:
    script = 'actions:\n  - when: "rf_items:..6 rf_length:..1799"\n    set: { comments: EP }\n'
    assert changes(beets_env, script, n=10) == {}


def test_ALBUM_FACTS_2_named_count(beets_env: BeetsEnv) -> None:
    script = 'actions:\n  - when: "rf_share_live_tracks:100"\n    set: { comments: live }\n'
    assert changes(beets_env, script) == {"comments": "live"}


def test_ALBUM_FACTS_1_facts_in_templates(beets_env: BeetsEnv) -> None:
    script = "actions:\n  - set: { comments: '$rf_items tracks' }\n"
    assert changes(beets_env, script) == {"comments": "5 tracks"}


@pytest.mark.parametrize(("artist", "joined"), [("A & B", True), ("AC/DC", False)])
def test_ALBUM_FACTS_8_rules_file(beets_env: BeetsEnv, artist: str, joined: bool) -> None:
    script = 'actions:\n  - when: "rf_joined_albumartist:1"\n    set: { comments: split? }\n'
    expected = {"comments": "split?"} if joined else {}
    assert changes(beets_env, script, albumartist=artist) == expected


def test_ALBUM_FACTS_7_rules_file_is_reread_when_it_changes(beets_env: BeetsEnv) -> None:
    (beets_env.beetsdir / "artist-names.yaml").write_text(RULES)
    backend = beets_env.backend(CONFIG)
    assert backend.artist_names().has_joiner("Simon & Garfunkel")
    path: Path = beets_env.beetsdir / "artist-names.yaml"
    path.write_text(RULES.replace("[AC/DC]", "[AC/DC, Simon & Garfunkel]"))
    os.utime(path, (1, 1))
    assert not backend.artist_names().has_joiner("Simon & Garfunkel")


def test_ALBUM_FACTS_2_bad_fact_name_stops_startup(beets_env: BeetsEnv) -> None:
    with pytest.raises(config.BeetsConfigError, match=r"album_facts\.items"):
        beets_env.backend("rifferr:\n  album_facts:\n    items: 'title:x'\n")
