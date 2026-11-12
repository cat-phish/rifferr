"""Pins the semi-internal beets behaviors BeetsBackend relies on (BEETS-IMPORT-2, -12).

Run these first when bumping beets.
"""

from __future__ import annotations

import os
from pathlib import Path

import beets
import beets.plugins
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rifferr.backends.beets import BeetsBackend, config, from_profile
from rifferr.core.profile import ProfileError, parse_profile
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BeetsEnv, seed


def test_BEETS_IMPORT_1_config_comes_from_profile_beetsdir(beets_env: BeetsEnv) -> None:
    beets_env.backend("ui:\n  color: no\n")
    assert os.environ["BEETSDIR"] == str(beets_env.beetsdir.resolve())
    assert beets.config["ui"]["color"].get() is False
    db, music = config.library_paths()
    assert db == beets_env.beetsdir / "library.db"
    assert music == beets_env.music


def test_BEETS_IMPORT_1_one_beetsdir_per_process(beets_env: BeetsEnv, tmp_path: Path) -> None:
    beets_env.backend()
    other = tmp_path / "other-beets"
    other.mkdir()
    with pytest.raises(config.BeetsConfigError, match="one profile"):
        config.load(other)
    config.load(beets_env.beetsdir)  # same dir again is fine


def test_BEETS_IMPORT_1_broken_config_is_a_clear_error(beets_env: BeetsEnv) -> None:
    (beets_env.beetsdir / "config.yaml").write_text("import: [unclosed\n")
    with pytest.raises(config.BeetsConfigError, match="can't read beets config"):
        config.load(beets_env.beetsdir)


def test_BEETS_IMPORT_2_overlay_is_removed_afterwards(beets_env: BeetsEnv) -> None:
    beets_env.backend()
    sources = list(beets.config.sources)
    with config.overlay({"import": {"write": True, "quiet": True}}):
        assert beets.config["import"]["write"].get() is True
        assert beets.config["import"]["copy"].get() is True  # merged, not replaced
    assert beets.config["import"]["write"].get() is False
    assert list(beets.config.sources) == sources
    assert "quiet: yes" not in (beets_env.beetsdir / "config.yaml").read_text()


def test_BEETS_IMPORT_2_plugins_load_from_user_config(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend("plugins: [inline]\nalbum_fields:\n  shout: album.upper()\n")
    assert [p.name for p in beets.plugins.find_plugins()] == ["inline"]
    seed(backend, [{"albumartist": "A", "album": "quiet", "items": [{"title": "t", "track": 1}]}])
    [album] = backend.query(Selection())
    assert backend.evaluate("$shout", album) == "QUIET"


def test_BEETS_IMPORT_9_plugin_template_functions(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    seed(backend, [{"albumartist": "A b", "album": "x", "items": [{"title": "t", "track": 1}]}])
    [album] = backend.query(Selection())
    assert backend.evaluate("%upper{$albumartist}", album) == "A B"
    assert backend.evaluate("%if{$year,yes,no}", album) == "no"


def test_BEETS_IMPORT_10_command_env(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    assert backend.command_env() == {"BEETSDIR": str(beets_env.beetsdir.resolve())}


def test_BEETS_IMPORT_12_regex_query_needs_quotes_for_backslashes(beets_env: BeetsEnv) -> None:
    """Pins B-018: beets splits query strings with shell rules."""
    backend = beets_env.backend()
    seed(
        backend, [{"albumartist": "A", "album": "First EP", "items": [{"title": "t", "track": 1}]}]
    )
    assert len(backend.query(Selection(query="'album::\\bEP\\b'"))) == 1
    assert backend.query(Selection(query="album::\\bEP\\b")) == []  # backslashes lost


def test_BEETS_IMPORT_15_entry_point_refuses_profile_without_beetsdir() -> None:
    profile = parse_profile("{}\n", "p.yaml", name="p", root=Path("/p"), env={})
    with pytest.raises(ProfileError, match=r"beets\.beetsdir"):
        from_profile(profile)


def test_BEETS_IMPORT_15_entry_point_loads_the_profiles_beetsdir(beets_env: BeetsEnv) -> None:
    beets_env.write_config()
    text = f"beets: {{ beetsdir: {beets_env.beetsdir} }}\n"
    profile = parse_profile(text, "p.yaml", name="p", root=Path("/p"), env={})
    assert isinstance(from_profile(profile), BeetsBackend)
    assert config.loaded_beetsdir() == beets_env.beetsdir.resolve()


@settings(max_examples=300, deadline=None)
@given(st.text())
def test_BEETS_IMPORT_13_escaped_text_survives_beets_templates(value: str) -> None:
    from beets.util.functemplate import Template

    from rifferr.backends.beets import escape_template

    assert Template(escape_template(value)).substitute({"album": "X"}, {}) == value
