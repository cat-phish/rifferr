"""S-SUBWORKFLOW in ``rifferr validate`` and ``rifferr trust`` (SUBWORKFLOW-1, -13, -14)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rifferr.appdb import AppDB
from rifferr.profile_store import app_db_path
from rifferr.trust import workflow_hash
from tests.cli.conftest import cli, wf

CALLED = wf("{id: get, uses: shell, with: {per: run, run: [curl, 'https://example.invalid']}}")
CALLER = wf("{id: sub, uses: workflow, with: {workflow: called.yaml}}")


def trusted(text: str) -> bool:
    db = AppDB(app_db_path())
    try:
        return db.is_trusted(workflow_hash(text))
    finally:
        db.close()


def files(tmp_path: Path, called: str = CALLED) -> Path:
    (tmp_path / "called.yaml").write_text(called)
    caller = tmp_path / "caller.yaml"
    caller.write_text(CALLER)
    return caller


def test_SUBWORKFLOW_1_validate_reports_errors_in_the_called_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    caller = files(tmp_path, wf("{id: x, uses: nosuch}"))
    assert cli("validate", str(caller)) == 1
    out = capsys.readouterr().out
    assert f"{tmp_path / 'called.yaml'}:3:" in out
    assert "unknown step type 'nosuch'" in out


def test_SUBWORKFLOW_14_validate_safety_includes_called_steps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    caller = files(tmp_path)
    assert cli("validate", "--safety", str(caller)) == 0
    assert "safety: [sub/get] (called.yaml) network_tool" in capsys.readouterr().out


def test_SUBWORKFLOW_13_trust_lists_and_trusts_called_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    caller = files(tmp_path)
    assert cli("trust", str(caller), "--yes") == 0
    out = capsys.readouterr().out
    assert f"called, not trusted: sha256 {workflow_hash(CALLED)}" in out
    assert f"trusted {tmp_path / 'called.yaml'}" in out
    assert trusted(CALLER)
    assert trusted(CALLED)
