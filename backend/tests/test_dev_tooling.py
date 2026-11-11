"""Repo-structure checks for F-DEV-TOOLING.

DEV-TOOLING-2 (all checks pass): ruff runs here because it is fast; pyright and
pytest are verified by running them, locally and in CI.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import rifferr
from rifferr import plugins
from rifferr.schemas import export

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
FRONTEND = REPO / "frontend"
PACKAGE = Path(rifferr.__file__).parent

LAYOUT = [
    "core",
    "engine",
    "tagscript",
    "steps",
    "covers",
    "ai",
    "backends",
    "backends/beets",
    "backends/files",
    "worker",
    "api",
]


def pyproject() -> dict[str, object]:
    return tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sub", LAYOUT)
def test_DEV_TOOLING_1_package_layout(sub: str) -> None:
    assert (PACKAGE / sub / "__init__.py").is_file()


def test_DEV_TOOLING_1_backend_base_module_and_python_floor() -> None:
    assert (PACKAGE / "backends" / "base.py").is_file()
    project = pyproject()["project"]
    assert isinstance(project, dict)
    assert project["name"] == "rifferr"
    assert project["requires-python"] == ">=3.12"
    assert (BACKEND / "uv.lock").is_file()


def test_DEV_TOOLING_1_entry_point_group_names_are_fixed() -> None:
    assert plugins.ALL_GROUPS == (
        "rifferr.steps",
        "rifferr.cover_providers",
        "rifferr.ai_providers",
        "rifferr.template_functions",
        "rifferr.backends",  # added by C-BACKEND
    )


def test_DEV_TOOLING_3_pyright_strict_on_pure_layers() -> None:
    tool = pyproject()["tool"]
    assert isinstance(tool, dict)
    cfg = tool["pyright"]
    assert cfg["typeCheckingMode"] == "basic"
    assert sorted(cfg["strict"]) == sorted(
        f"src/rifferr/{layer}" for layer in ("core", "engine", "tagscript")
    )


def package_json() -> dict[str, object]:
    return json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("script", ["dev", "build", "lint", "typecheck", "test", "gen:api"])
def test_DEV_TOOLING_9_frontend_scripts(script: str) -> None:
    scripts = package_json()["scripts"]
    assert isinstance(scripts, dict)
    assert script in scripts


def test_DEV_TOOLING_9_typescript_strict() -> None:
    cfg = json.loads((FRONTEND / "tsconfig.app.json").read_text(encoding="utf-8"))
    assert cfg["compilerOptions"]["strict"] is True


def test_DEV_TOOLING_10_gen_api_uses_openapi_typescript_on_exported_document() -> None:
    scripts = package_json()["scripts"]
    assert isinstance(scripts, dict)
    cmd = str(scripts["gen:api"])
    assert "openapi-typescript" in cmd
    assert "schemas/openapi.json" in cmd
    assert "openapi.json" in export.render()


CI = REPO / ".github" / "workflows" / "ci.yml"


@pytest.mark.parametrize(
    "needle",
    [
        "gitleaks",
        "spec.py lint",
        "ruff format --check",
        "ruff check",
        "pyright",
        "pytest",
        "pnpm lint",
        "pnpm typecheck",
        "pnpm test",
        "schemas.export --check",
    ],
)
def test_DEV_TOOLING_11_ci_runs_every_check(needle: str) -> None:
    assert needle in CI.read_text(encoding="utf-8")


def test_DEV_TOOLING_11_ci_python_matrix() -> None:
    text = CI.read_text(encoding="utf-8")
    assert '"3.12"' in text
    assert '"3.14"' in text
    assert "pull_request" in text
    assert "push" in text


@pytest.mark.parametrize(
    "hook", ["gitleaks", "spec-lint", "ruff-format", "ruff-check", "eslint", "prettier"]
)
def test_DEV_TOOLING_12_pre_commit_hooks(hook: str) -> None:
    text = (REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert f"id: {hook}" in text


def test_DEV_TOOLING_13_committed_schemas_are_current() -> None:
    assert export.stale(REPO / "schemas", export.render()) == []


def test_DEV_TOOLING_13_stale_detection(tmp_path: Path) -> None:
    files = export.render()
    assert sorted(export.stale(tmp_path, files)) == sorted(files)
    export.write(tmp_path, files)
    assert export.stale(tmp_path, files) == []
    (tmp_path / "openapi.json").write_text("{}", encoding="utf-8")
    (tmp_path / "old.schema.json").write_text("{}", encoding="utf-8")
    assert sorted(export.stale(tmp_path, files)) == ["old.schema.json", "openapi.json"]


@pytest.mark.parametrize("args", [["format", "--check"], ["check"]])
def test_DEV_TOOLING_2_ruff_passes(args: list[str]) -> None:
    ruff = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")
    result = subprocess.run(
        [ruff, *args, "."], cwd=BACKEND, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
