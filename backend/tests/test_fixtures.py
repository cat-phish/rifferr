from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
COMMITTED = Path(__file__).parent / "fixtures" / "audio"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "make_fixtures", REPO / "scripts/make_fixtures.py"
    )
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_fixtures"] = module
    spec.loader.exec_module(module)
    return module


def _ffmpeg_works() -> bool:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    return subprocess.run(["ffprobe", "-version"], capture_output=True).returncode == 0


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_works(), reason="ffmpeg/ffprobe not runnable")


def _tags(path: Path) -> dict[str, str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    tags: dict[str, str] = json.loads(out)["format"].get("tags", {})
    return {k.lower(): v for k, v in tags.items()}


@needs_ffmpeg
def test_DEV_TOOLING_8_fixtures_have_known_tags_and_regenerate_identically(tmp_path: Path) -> None:
    mf = _load_script()
    first = mf.generate(tmp_path / "a")
    second = mf.generate(tmp_path / "b")
    for fx, a, b in zip(mf.FIXTURES, first, second, strict=True):
        assert _tags(a) == _tags(b)
        for key, value in fx.tags.items():
            assert _tags(a)[key] == value, (fx.path, key)


def test_DEV_TOOLING_8_committed_fixtures_exist_and_are_tiny() -> None:
    mf = _load_script()
    for fx in mf.FIXTURES:
        path = COMMITTED / fx.path
        assert path.is_file(), f"missing {path}; run scripts/make_fixtures.py"
        assert path.stat().st_size < 64 * 1024
