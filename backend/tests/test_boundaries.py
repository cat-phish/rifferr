from __future__ import annotations

from pathlib import Path

import pytest

import rifferr
from tests.boundaries import check

PACKAGE_ROOT = Path(rifferr.__file__).parent


def make_pkg(root: Path, files: dict[str, str]) -> Path:
    pkg = root / "rifferr"
    for rel, src in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    return pkg


def test_DEV_TOOLING_4_repo_respects_import_boundaries() -> None:
    assert [str(v) for v in check(PACKAGE_ROOT)] == []


@pytest.mark.parametrize(
    "rel",
    ["engine/x.py", "api/x.py", "worker/x.py", "backends/base.py", "backends/files/x.py"],
)
def test_DEV_TOOLING_4_beets_import_outside_beets_backend_is_flagged(
    tmp_path: Path, rel: str
) -> None:
    pkg = make_pkg(tmp_path, {rel: "import beets.library\n"})
    assert [v.rule for v in check(pkg)] == ["only rifferr.backends.beets may import beets"]


def test_DEV_TOOLING_4_beets_backend_may_import_beets(tmp_path: Path) -> None:
    pkg = make_pkg(tmp_path, {"backends/beets/lib.py": "from beets import library\n"})
    assert check(pkg) == []


@pytest.mark.parametrize("layer", ["core", "engine", "tagscript", "steps", "covers", "ai"])
@pytest.mark.parametrize(
    "src",
    [
        "from rifferr.backends.beets import BeetsBackend\n",
        "from rifferr.backends import beets\n",
        "from ..backends.files import x\n",
    ],
)
def test_DEV_TOOLING_4_backend_implementation_import_is_flagged(
    tmp_path: Path, layer: str, src: str
) -> None:
    pkg = make_pkg(tmp_path, {f"{layer}/x.py": src})
    rules = {v.rule for v in check(pkg)}
    assert rules == {f"rifferr.{layer} may import only rifferr.backends.base"}


@pytest.mark.parametrize(
    "src",
    [
        "from rifferr.backends.base import LibraryBackend\n",
        "from rifferr.backends import base\n",
        "from ..backends.base import LibraryBackend\n",
    ],
)
def test_DEV_TOOLING_4_backends_base_is_allowed(tmp_path: Path, src: str) -> None:
    pkg = make_pkg(tmp_path, {"engine/x.py": src})
    assert check(pkg) == []


@pytest.mark.parametrize("layer", ["core", "engine", "tagscript"])
@pytest.mark.parametrize(
    "src",
    [
        "import httpx\n",
        "import socket\n",
        "import subprocess\n",
        "import requests\n",
        "from urllib.request import urlopen\n",
        "from asyncio import subprocess\n",
    ],
)
def test_DEV_TOOLING_5_pure_layer_io_import_is_flagged(
    tmp_path: Path, layer: str, src: str
) -> None:
    pkg = make_pkg(tmp_path, {f"{layer}/x.py": src})
    assert [v.rule for v in check(pkg)] == [
        f"rifferr.{layer} is pure: no network or process modules"
    ]


@pytest.mark.parametrize("layer", ["steps", "covers", "ai", "worker", "api"])
def test_DEV_TOOLING_5_non_pure_layers_may_do_io(tmp_path: Path, layer: str) -> None:
    pkg = make_pkg(tmp_path, {f"{layer}/x.py": "import subprocess\nimport httpx\n"})
    assert check(pkg) == []
