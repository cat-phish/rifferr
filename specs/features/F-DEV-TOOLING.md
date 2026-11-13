# F-DEV-TOOLING: Project skeleton, checks and CI
Status: Verified
Milestone: M0

## Context
Every later spec assumes the commands in AGENTS.md ("Commands") work: `uv run pytest`,
`uv run pyright`, `pnpm test`, and so on. This spec creates the backend and
frontend projects, the quality gates, the audio fixture generator and CI, with
no application behavior. Architecture §9 (layout) and §10 (tooling).

## Behavior
- DEV-TOOLING-1: `backend/` MUST be a uv project (`pyproject.toml`, `uv.lock`) with package `rifferr` under `backend/src/rifferr/`, requiring Python `>=3.12`, and the sub-packages listed in architecture §9 (`core`, `engine`, `tagscript`, `steps`, `covers`, `ai`, `backends/{base.py,beets,files}`, `worker`, `api`), each with an `__init__.py`.
- DEV-TOOLING-2: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright` and `uv run pytest` MUST all pass on the empty skeleton.
- DEV-TOOLING-3: pyright MUST run in `strict` mode for `rifferr.core`, `rifferr.engine` and `rifferr.tagscript`, and `basic` mode elsewhere.
- DEV-TOOLING-4: An import-boundary test MUST fail if any module outside `rifferr.backends.beets` imports `beets`, or if `core`/`engine`/`tagscript`/`steps`/`covers`/`ai` import anything under `rifferr.backends` other than `rifferr.backends.base`.
- DEV-TOOLING-5: A test MUST fail if `core`, `engine` or `tagscript` import network or process modules (`httpx`, `socket`, `subprocess`, `requests`, `urllib.request`).
- DEV-TOOLING-6: The test suite MUST refuse to run (fail at session start) if `BEETSDIR` is unset in a test that needs beets, and MUST set `HOME`, `XDG_CONFIG_HOME` and `BEETSDIR` to directories under `tmp_path` for every test, so no test can read the developer's real beets config.
- DEV-TOOLING-7: Network access MUST be blocked during tests (a socket guard fixture); a test that opens a real connection fails.
- DEV-TOOLING-8: `scripts/make_fixtures.py` MUST generate tiny FLAC and MP3 files (≤ 1 s of silence or a tone, via ffmpeg) with known tags into `backend/tests/fixtures/audio/`, and running it twice MUST produce files with identical tags.
- DEV-TOOLING-9: `frontend/` MUST be a pnpm + Vite + React + TypeScript project where `pnpm lint`, `pnpm typecheck` and `pnpm test` pass on the empty skeleton, with `strict: true` in `tsconfig`.
- DEV-TOOLING-10: `pnpm gen:api` MUST regenerate TypeScript API types from the backend's OpenAPI document (served or dumped to a file) using openapi-typescript.
- DEV-TOOLING-11: CI MUST run, on every push and pull request: gitleaks, `spec.py lint`, backend format/lint/type/test, frontend lint/typecheck/test. Playwright e2e runs in CI once F-UI-SHELL exists.
- DEV-TOOLING-12: `.pre-commit-config.yaml` MUST add ruff (format + check) for `backend/` and eslint/prettier for `frontend/`, alongside the existing gitleaks and spec-lint hooks.
- DEV-TOOLING-13: `uv run python -m rifferr.schemas.export` MUST exist and write JSON Schemas into `schemas/`; a test MUST fail if committed schemas differ from freshly generated ones.

## Interfaces
```
backend/
  pyproject.toml          # [project] rifferr, [tool.ruff], [tool.pyright], [tool.pytest.ini_options]
  src/rifferr/...         # empty packages per architecture §9
  src/rifferr/schemas/export.py
  tests/conftest.py       # tmp HOME/XDG/BEETSDIR, socket guard
  tests/test_boundaries.py
  tests/fixtures/audio/
scripts/make_fixtures.py
frontend/
  package.json            # scripts: dev, build, lint, typecheck, test, e2e, gen:api
  vite.config.ts  tsconfig.json  src/main.tsx
.github/workflows/ci.yml
```
Entry-point groups are declared (empty) now so later specs only add entries:
`rifferr.steps`, `rifferr.cover_providers`, `rifferr.ai_providers`, `rifferr.template_functions`.

## Examples
```bash
cd backend && uv sync && uv run pytest        # 0 app tests, boundary tests pass
echo 'import beets' >> src/rifferr/engine/x.py && uv run pytest tests/test_boundaries.py   # fails
```

## Extensibility
Entry-point group names are fixed here and become public API. Tool versions are pinned in lock files.

## Security & safety
- Test isolation (DEV-TOOLING-6/7) protects the developer's real library.
- CI runs gitleaks on the full history of each push.

## Non-goals
- Docker image and PyPI packaging (F-PACKAGING, M5).
- Any application behavior.

## Open questions
- ~~CI host?~~ Resolved 2026-11-11 (author): GitHub Actions.
- ~~Python CI matrix?~~ Resolved 2026-11-11 (author): 3.12 and 3.14.
- ~~Which dependencies are approved?~~ Resolved 2026-11-11 (author): all tooling deps — ruff, pyright, pytest, pytest-asyncio, hypothesis, respx, pydantic; vite, react, react-dom, typescript, eslint, prettier, vitest, @testing-library/react, jsdom, openapi-typescript. Runtime deps (fastapi, beets, …) wait for their own specs.

## Tasks
CI workflow is written but has not run yet: the repo has no GitHub remote (BACKLOG B-014).
- [x] backend uv project and empty packages
- [x] ruff, pyright config (strict zones)
- [x] conftest isolation + socket guard
- [x] boundary tests
- [x] make_fixtures.py + fixtures
- [x] schema export module + drift test
- [x] frontend Vite project, eslint, prettier, vitest
- [x] gen:api script
- [x] CI workflow
- [x] pre-commit ruff/eslint hooks
