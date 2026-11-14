"""F-TRIGGERS CLI: ``rifferr run --input`` and ``rifferr webhook``."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rifferr.appdb import AppDB, AppTriggers
from rifferr.profile_store import app_db_path
from tests.cli.conftest import Server, add_workflow, cli, make_profile, wf

SHOW_INPUT = wf("{id: show, uses: shell, with: {per: run, run: [echo, 'input={{ run.input }}']}}")


def test_TRIGGERS_7_run_input_is_made_absolute(
    server: Server,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_workflow("show", SHOW_INPUT, trusted=True)
    (tmp_path / "Album").mkdir()
    monkeypatch.chdir(tmp_path)
    assert cli("run", "show", "--profile", "main", "--input", "Album", "--follow") == 0
    out = capsys.readouterr().out
    assert f"input={os.path.join(tmp_path, 'Album')}" in out


def _hash(name: str) -> str | None:
    db = AppDB(app_db_path())
    try:
        return AppTriggers(db).webhook_hash("main", name)
    finally:
        db.close()


def test_TRIGGERS_17_webhook_new_rotate_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_profile(tmp_path)
    assert cli("webhook", "rotate", "dl", "--profile", "main") == 2  # nothing to rotate
    assert cli("webhook", "new", "dl", "--profile", "main") == 0
    out = capsys.readouterr().out
    assert "POST /hooks/main/dl" in out
    secret = next(line.split()[-1] for line in out.splitlines() if line.startswith("secret:"))
    first = _hash("dl")
    assert first is not None
    assert secret not in first  # only the hash is stored
    assert cli("webhook", "new", "dl", "--profile", "main") == 2  # already exists
    assert "rotate" in capsys.readouterr().err
    assert cli("webhook", "rotate", "dl", "--profile", "main") == 0
    capsys.readouterr()
    assert _hash("dl") not in (None, first)
    assert cli("webhook", "list") == 0
    listing = capsys.readouterr().out
    assert "main\tdl\t/hooks/main/dl" in listing
    assert secret not in listing


@pytest.mark.parametrize(("name", "profile"), [("a/b", "main"), ("dl", "nope")])
def test_TRIGGERS_17_webhook_new_rejects_bad_input(tmp_path: Path, name: str, profile: str) -> None:
    make_profile(tmp_path)
    assert cli("webhook", "new", name, "--profile", profile) == 2
