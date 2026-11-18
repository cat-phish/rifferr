# ruff: noqa: F811
"""SEC-62 follow-ups: grant, scope and invalidation of beets-config trust."""

from __future__ import annotations

from pathlib import Path

import pytest

from rifferr.backends.beets import config
from rifferr.beets_config import inspect_config
from rifferr.cli.main import main
from rifferr.profile_store import app_db_path, config_root, create_profile
from tests.api.conftest import Api, api  # noqa: F401
from tests.backends.beets.conftest import BeetsEnv, beets_env  # noqa: F401


def _code_config(env: BeetsEnv, extra: str = "plugins: [inline]\n") -> None:
    (env.beetsdir / "config.yaml").write_text(
        f"directory: {env.music}\nlibrary: {env.beetsdir / 'library.db'}\n{extra}"
    )


def test_inspect_lists_code_keys_and_follows_includes(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("include: [more.yaml]\nplugins: [fetchart]\n")
    assert not inspect_config(tmp_path).needs_trust
    (tmp_path / "more.yaml").write_text("pluginpath: [/x]\n")
    report = inspect_config(tmp_path)
    assert report.needs_trust
    assert report.files == ["config.yaml", "more.yaml"]
    assert any("more.yaml: pluginpath" in k for k in report.code_keys)


def test_inline_and_hook_need_trust_and_a_change_invalidates_it(
    beets_env: BeetsEnv,
) -> None:
    _code_config(beets_env)
    sha = inspect_config(beets_env.beetsdir).sha256
    config.reset()
    with pytest.raises(config.BeetsConfigError, match="not trusted"):
        config.load(beets_env.beetsdir)
    with pytest.raises(config.BeetsConfigError, match="not trusted"):
        config.load(beets_env.beetsdir, lambda s: s == "other")
    _code_config(beets_env, "plugins: [inline]\nverbose: 1\n")
    assert inspect_config(beets_env.beetsdir).sha256 != sha
    config.load(beets_env.beetsdir, lambda s: s == inspect_config(beets_env.beetsdir).sha256)
    config.reset()


async def test_api_shows_and_grants_beets_config_trust(api: Api, beets_env: BeetsEnv) -> None:
    create_profile(config_root(), "bp", beets_env.beetsdir)
    _code_config(beets_env, "plugins: [hook]\n")
    got = (await api.client.get("/api/profiles/bp/beets-config")).json()
    assert (got["needs_trust"], got["trusted"]) == (True, False)
    bad = await api.client.post("/api/profiles/bp/beets-config/trust", json={"sha256": "0" * 64})
    assert bad.status_code == 409
    ok = await api.client.post(
        "/api/profiles/bp/beets-config/trust", json={"sha256": got["sha256"]}
    )
    assert ok.json()["trusted"] is True
    assert api.state.db.is_trusted(got["sha256"], "bp")
    assert not api.state.db.is_trusted(got["sha256"], "main")


def test_cli_trust_config(beets_env: BeetsEnv) -> None:
    create_profile(config_root(), "bp", beets_env.beetsdir)
    _code_config(beets_env)
    assert main(["trust-config", "bp", "--yes"]) == 0
    from rifferr.appdb import AppDB

    db = AppDB(app_db_path())
    assert db.is_trusted(inspect_config(beets_env.beetsdir).sha256, "bp")
    db.close()
