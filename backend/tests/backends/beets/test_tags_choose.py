"""S-TAGS-CHOOSE on beets: ``%genre_root`` (TAGS-CHOOSE-9) and "This is one artist"
writing the artist-name rules (TAGS-CHOOSE-13)."""

from __future__ import annotations

import pytest

from rifferr.backends.beets.genres import genre_root, tree_path
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BeetsEnv, seed

RULES = '# known bands\njoiners: [" & ", "/"]\nnames: [Simon & Garfunkel]  # duo\n'


def test_TAGS_CHOOSE_9_lastgenre_ships_its_genre_tree() -> None:
    path = tree_path()
    assert path is not None
    assert path.name == "genres-tree.yaml"


@pytest.mark.parametrize(
    ("value", "root"),
    [
        ("Deep House", "Electronic"),
        ("post-punk", "Rock"),
        ("Rock", "Rock"),
        ("Hip Hop", "Hip Hop"),
        ("Not A Genre", "Not A Genre"),
    ],
)
def test_TAGS_CHOOSE_9_genre_root(value: str, root: str) -> None:
    assert genre_root(value) == root


def test_TAGS_CHOOSE_9_genre_root_in_beets_templates(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend("")
    seed(backend, [{"albumartist": "A", "album": "X", "items": [{"title": "t"}]}])
    (album,) = backend.query(Selection())
    assert backend.evaluate("%genre_root{Deep House}", album) == "Electronic"


def test_TAGS_CHOOSE_13_add_artist_name_keeps_comments(beets_env: BeetsEnv) -> None:
    path = beets_env.beetsdir / "artist-names.yaml"
    path.write_text(RULES)
    backend = beets_env.backend("rifferr:\n  artist_names: artist-names.yaml\n")
    assert backend.artist_names().has_joiner("AC/DC")
    assert "added 'AC/DC'" in backend.add_artist_name("AC/DC")
    text = path.read_text()
    assert text.startswith("# known bands")
    assert "# duo" in text
    assert not backend.artist_names().has_joiner("AC/DC")
    assert "already" in backend.add_artist_name("AC/DC")


def test_TAGS_CHOOSE_13_without_a_rules_file_says_how_to_set_one(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend("")
    with pytest.raises(ValueError, match=r"rifferr\.artist_names"):
        backend.add_artist_name("AC/DC")
