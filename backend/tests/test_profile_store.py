from __future__ import annotations

import subprocess
import sys
import zoneinfo
from datetime import tzinfo
from pathlib import Path

import pytest

from rifferr.core.profile import ProfileError, ProfileLoadError
from rifferr.profile_store import (
    config_root,
    create_profile,
    effective_timezone,
    list_profiles,
    load_profile,
)


def test_PROFILE_1_config_root_precedence(tmp_path: Path) -> None:
    assert config_root({"RIFFERR_CONFIG_DIR": "/config", "XDG_CONFIG_HOME": "/x"}) == Path(
        "/config"
    )
    assert config_root({"XDG_CONFIG_HOME": "/x"}) == Path("/x/rifferr")
    assert config_root({"HOME": str(tmp_path)}) == tmp_path / ".config" / "rifferr"


def test_PROFILE_1_profile_lives_in_profiles_dir(tmp_path: Path) -> None:
    create_profile(tmp_path, "main")
    assert (tmp_path / "profiles" / "main" / "profile.yaml").is_file()
    p = load_profile(tmp_path, "main")
    assert p.root == tmp_path / "profiles" / "main"


def test_PROFILE_1_list_profiles(tmp_path: Path) -> None:
    assert list_profiles(tmp_path) == []
    create_profile(tmp_path, "b")
    create_profile(tmp_path, "a")
    (tmp_path / "profiles" / "not-a-profile").mkdir()
    (tmp_path / "profiles" / "Bad Name").mkdir()
    (tmp_path / "profiles" / "Bad Name" / "profile.yaml").write_text("{}\n")
    assert list_profiles(tmp_path) == ["a", "b"]


def test_PROFILE_2_load_reports_file_path_in_errors(tmp_path: Path) -> None:
    d = tmp_path / "profiles" / "main"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text("bogus: 1\n")
    with pytest.raises(ProfileLoadError) as exc:
        load_profile(tmp_path, "main")
    assert str(exc.value.errors[0]) == f"{d / 'profile.yaml'}:1:1: bogus: unknown key"


def test_PROFILE_2_beetsdir_tilde_is_expanded(tmp_path: Path) -> None:
    d = tmp_path / "profiles" / "main"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text("beets: {beetsdir: ~/beets}\n")
    p = load_profile(tmp_path, "main", env={"HOME": "/home/u"})
    assert p.beets.beetsdir == "/home/u/beets"


def test_PROFILE_3_load_rejects_bad_names(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_profile(tmp_path, "../etc")
    with pytest.raises(ProfileError):
        create_profile(tmp_path, "Main")


def test_PROFILE_3_missing_profile(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="no profile 'nope'"):
        load_profile(tmp_path, "nope")


def test_PROFILE_8_create_without_beetsdir_is_empty(tmp_path: Path) -> None:
    root = create_profile(tmp_path, "main")
    assert sorted(p.name for p in root.iterdir()) == ["profile.yaml", "scripts", "workflows"]
    p = load_profile(tmp_path, "main")
    assert p.vars == {}
    assert p.secrets == {}
    assert p.beets.beetsdir is None
    with pytest.raises(ProfileError, match=r"beets\.beetsdir"):
        p.require_beetsdir()


def test_PROFILE_8_create_with_existing_beetsdir_leaves_it_untouched(tmp_path: Path) -> None:
    beets = tmp_path / "beets"
    beets.mkdir()
    (beets / "config.yaml").write_text("directory: /music\n")
    before = {f.name: f.read_bytes() for f in beets.iterdir()}
    create_profile(tmp_path / "cfg", "main", beetsdir=beets)
    assert {f.name: f.read_bytes() for f in beets.iterdir()} == before
    assert load_profile(tmp_path / "cfg", "main").require_beetsdir() == beets


def test_PROFILE_8_create_refuses_missing_beetsdir_and_existing_profile(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not a directory"):
        create_profile(tmp_path, "main", beetsdir=tmp_path / "nope")
    create_profile(tmp_path, "main")
    with pytest.raises(ProfileError, match="already exists"):
        create_profile(tmp_path, "main")


def test_PROFILE_9_loading_a_profile_does_not_import_beets(tmp_path: Path) -> None:
    create_profile(tmp_path, "main")
    code = (
        "import sys; from pathlib import Path; from rifferr.profile_store import load_profile; "
        f"load_profile(Path({str(tmp_path)!r}), 'main'); "
        "assert not any(m == 'beets' or m.startswith('beets.') for m in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_PROFILE_10_timezone_override_and_system_default(tmp_path: Path) -> None:
    d = tmp_path / "profiles" / "tz"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text("timezone: Europe/Amsterdam\n")
    assert effective_timezone(load_profile(tmp_path, "tz")) == zoneinfo.ZoneInfo("Europe/Amsterdam")
    create_profile(tmp_path, "sys")
    assert isinstance(effective_timezone(load_profile(tmp_path, "sys")), tzinfo)


def test_PROFILE_10_invalid_timezone_is_an_error(tmp_path: Path) -> None:
    d = tmp_path / "profiles" / "tz"
    d.mkdir(parents=True)
    (d / "profile.yaml").write_text("timezone: Mars/Olympus\n")
    with pytest.raises(ProfileLoadError) as exc:
        load_profile(tmp_path, "tz")
    assert "Mars/Olympus" in str(exc.value)
    assert "timezone" in str(exc.value)


def test_TAGSCRIPT_1_profile_create_adds_scripts_folder(tmp_path: Path) -> None:
    create_profile(tmp_path, "main")
    assert (tmp_path / "profiles" / "main" / "scripts").is_dir()
