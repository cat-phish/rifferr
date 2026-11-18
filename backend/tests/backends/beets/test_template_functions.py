"""Rifferr template functions inside beets (C-TEMPLATE-FUNCTIONS). Contract tests."""

from __future__ import annotations

import subprocess
from importlib.metadata import EntryPoint
from pathlib import Path
from typing import Any

import beets.plugins
import pytest

from rifferr.backends.base import TemplateError
from rifferr.backends.beets import config, templates
from rifferr.backends.beets.config import BeetsConfigError
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BeetsEnv, seed

ALBUM = [{"albumartist": "A", "album": "x", "year": 2000, "items": [{"title": "t", "track": 1}]}]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(templates, "_cache", None)


def test_TEMPLATE_FUNCTIONS_1_entry_points_register_in_beets(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    seed(backend, ALBUM)
    [album] = backend.query(Selection())
    assert backend.evaluate("%ifgreater{$year,1999,new,old}", album) == "new"
    assert backend.evaluate("%ifgreater{$year,2000,new,old}", album) == "old"


def test_TEMPLATE_FUNCTIONS_2_one_beets_plugin_named_rifferr(beets_env: BeetsEnv) -> None:
    beets_env.backend()  # plugins: [] in the config; the worker adds rifferr itself
    [plugin] = beets.plugins.find_plugins()
    assert plugin.name == "rifferr"
    assert isinstance(plugin, templates.RifferrTemplatePlugin)
    assert "ifgreater" in plugin.template_funcs
    assert "rifferr" in config.plugin_names()


def test_TEMPLATE_FUNCTIONS_2_listed_plugin_is_not_loaded_twice(beets_env: BeetsEnv) -> None:
    beets_env.write_config()
    path = beets_env.beetsdir / "config.yaml"
    path.write_text(path.read_text().replace("plugins: []", "plugins: [rifferr]"))
    config.load(beets_env.beetsdir, lambda _sha: True)
    assert [p.name for p in beets.plugins.find_plugins()] == ["rifferr"]


def test_TEMPLATE_FUNCTIONS_3_beet_cli_evaluates_the_same(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    seed(backend, ALBUM)
    [album] = backend.query(Selection())
    fmt = "%ifgreater{$year,1999,new,old}"
    argv = backend.cli_command("ls", ["-a", "-f", fmt], Selection(query="album:x"))
    out = subprocess.run(
        argv, env=dict(backend.command_env()), capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == backend.evaluate(fmt, album) == "new"


def _ep(name: str, value: str) -> EntryPoint:
    return EntryPoint(name=name, value=value, group="rifferr.template_functions")


def test_TEMPLATE_FUNCTIONS_4_two_providers_of_one_name_fail_startup(
    beets_env: BeetsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    eps = [_ep("dup", "pkg_a:f"), _ep("dup", "pkg_b:f")]
    monkeypatch.setattr(templates, "entry_points", lambda group: eps)
    with pytest.raises(BeetsConfigError, match=r"%dup .*pkg_a:f.*pkg_b:f"):
        beets_env.backend()
    assert config.loaded_beetsdir() is None


def test_TEMPLATE_FUNCTIONS_4_collision_with_beets_builtin_fails_startup(
    beets_env: BeetsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(templates, "_cache", {"upper": str.upper})
    with pytest.raises(BeetsConfigError, match=r"%upper .*beets and the beets plugin rifferr"):
        beets_env.backend()


def test_TEMPLATE_FUNCTIONS_4_collision_with_other_plugin_fails_startup(
    beets_env: BeetsEnv, tmp_path: Path
) -> None:
    plugdir = tmp_path / "plug"
    plugdir.mkdir()
    (plugdir / "otherfuncs.py").write_text(
        "from beets.plugins import BeetsPlugin\n"
        "class Other(BeetsPlugin):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "        self.template_funcs['ifgreater'] = lambda *a: ''\n"
    )
    beets_env.write_config()
    path = beets_env.beetsdir / "config.yaml"
    path.write_text(
        path.read_text().replace("plugins: []", f"pluginpath: ['{plugdir}']\nplugins: [otherfuncs]")
    )
    with pytest.raises(BeetsConfigError, match=r"%ifgreater .*otherfuncs.*rifferr"):
        config.load(beets_env.beetsdir, lambda _sha: True)


def _boom(*_args: Any) -> str:
    raise ValueError("bad input")


def test_TEMPLATE_FUNCTIONS_5_exception_names_the_function(
    beets_env: BeetsEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(templates, "_cache", {"boom": templates._wrap("boom", _boom)})  # pyright: ignore[reportPrivateUsage]
    backend = beets_env.backend()
    seed(backend, ALBUM)
    [album] = backend.query(Selection())
    with pytest.raises(TemplateError, match=r"%boom failed: bad input"):
        backend.evaluate("%boom{$year}", album)


def test_TEMPLATE_FUNCTIONS_5_builtin_module_is_pure() -> None:
    import rifferr.tagscript.functions as mod

    imported = {n for n, v in vars(mod).items() if type(v).__name__ == "module"}
    assert imported <= {"math"}
