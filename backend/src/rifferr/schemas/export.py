"""Regenerate ``schemas/`` from the Pydantic models.

uv run python -m rifferr.schemas.export          # write files
uv run python -m rifferr.schemas.export --check  # exit 1 if files are stale
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rifferr import __version__
from rifferr.schemas import PUBLISHED

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_OUT = REPO_ROOT / "schemas"


def openapi_document() -> dict[str, Any]:
    """The API's OpenAPI document. A stub until F-API provides the FastAPI app."""
    return {
        "openapi": "3.1.0",
        "info": {"title": "Rifferr", "version": __version__},
        "paths": {},
    }


def render() -> dict[str, str]:
    """Map of file name -> file content for everything under ``schemas/``."""
    files: dict[str, str] = {}
    for stem, model in sorted(PUBLISHED.items()):
        files[f"{stem}.schema.json"] = _dump(model.model_json_schema())
    files["openapi.json"] = _dump(openapi_document())
    return files


def _dump(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def stale(out: Path, files: dict[str, str]) -> list[str]:
    """Names of files that are missing, different, or no longer generated."""
    problems = [n for n, c in files.items() if _read(out / n) != c]
    if out.exists():
        problems += [p.name for p in sorted(out.glob("*.json")) if p.name not in files]
    return problems


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def write(out: Path, files: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for p in out.glob("*.json"):
        if p.name not in files:
            p.unlink()
    for name, content in files.items():
        (out / name).write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="fail if files are stale")
    args = ap.parse_args(argv)
    files = render()
    if args.check:
        problems = stale(args.out, files)
        for name in problems:
            print(f"stale: {args.out / name}", file=sys.stderr)
        if problems:
            print("run: uv run python -m rifferr.schemas.export", file=sys.stderr)
        return 1 if problems else 0
    write(args.out, files)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
