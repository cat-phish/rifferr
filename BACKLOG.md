# Backlog

Ideas, issues, tech debt and open questions that come up while building and
are **not** part of the current task. Append here instead of acting on them.

## Rules
- One entry per line, in the section that fits:
  `- B-<NNN> [idea|bug|debt|question] (YYYY-MM-DD, from <spec ID or "session">) Summary.`
  Extra detail goes on indented lines below the entry.
- IDs are never reused. Take the next number after the highest in the file.
- When an entry is picked up, turn it into a spec, a spec *Open question*, or a
  spec task, then move it to **Done** with a pointer (`→ S-SHELL`, `→ 1a2b3c4`).
- List or filter: `python3 scripts/spec.py backlog [--tag question] [--section Ideas]`.
  `spec.py lint` checks IDs are unique and well-formed.

## Ideas
- B-001 [idea] (2026-11-11, from architecture §12) Deeper *arr integration as an `integrations` plugin type.
  Examples: tell Lidarr to rescan after import, read Lidarr's wanted list to flag incomplete albums, refresh Navidrome/Jellyfin after a run.
- B-002 [idea] (2026-11-11, from session) Generated, read-only HTML dashboard of spec status and reference-workflow coverage (`spec.py html`).
- B-003 [idea] (2026-11-11, from architecture §12) Desktop wrapper (pywebview or Tauri).

- B-019 [idea] (2026-11-11, from F-WORKER) Notify on worker lifecycle events that matter for unattended imports: crash, hang, refusal to start, and (optionally) idle exit. Hook into S-NOTIFY (M2) once it exists.

## Issues
- B-004 [bug] (2026-11-11, from session) Nix `pre-commit` package fails to build Python hook environments (`ModuleNotFoundError: No module named 'packaging'` in its virtualenv). Use the distro package or pipx; mention in contributor docs.
- B-012 [bug] (2026-11-11, from F-DEV-TOOLING) The dev shell exports `LD_LIBRARY_PATH` pointing at a Nix alsa-lib, which breaks system `ffmpeg`/`ffprobe` (`GLIBC_2.43 not found`). The ffmpeg fixture test skips locally; run `env -u LD_LIBRARY_PATH uv run pytest` or fix the shell profile.
- B-018 [bug] (2026-11-11, from C-BACKEND) beets splits query strings with shell rules, so the architecture §4 example `'albumtype:ep ^album::\bEP\b'` likely loses its backslashes (pattern becomes `bEPb`). Confirm with the BeetsBackend contract run (S-BEETS-IMPORT); if confirmed, fix the docs example to quote the regex term and consider a UI hint.

## Tech debt
- B-005 [debt] (2026-11-11, from session) No LICENSE file yet; add the AGPL-3.0-or-later text.
- B-007 [debt] (2026-11-11, from session) Decide a cadence for `pre-commit autoupdate` (or let Renovate/Dependabot do it).
- B-014 [debt] (2026-11-11, from F-DEV-TOOLING) No GitHub remote yet, so `.github/workflows/ci.yml` has never run. Push and confirm CI is green, then mark F-DEV-TOOLING Verified.
- B-015 [debt] (2026-11-11, from F-DEV-TOOLING) TypeScript pinned to 6.0 because typescript-eslint 8.x doesn't support TS 7; openapi-typescript 7 still declares a TS 5 peer (works with 6). Revisit when they catch up.
- B-016 [debt] (2026-11-11, from F-DEV-TOOLING) `schemas/openapi.json` is a stub until F-API provides the FastAPI app; `export.openapi_document()` must switch to `app.openapi()` then.
- B-017 [debt] (2026-11-11, from C-WORKFLOW) WORKFLOW-5 checks `with:` values before interpolation, so a non-string parameter written as an expression (e.g. `timeout: "{{ vars.t }}"`) fails validation. Decide whether typed params may take `{{ }}` and validate them after interpolation.

## Questions
- B-008 [question] (2026-11-11, from architecture §12) Which UI component library (shadcn/ui, Mantine, …)? Needed for F-UI-SHELL.
- B-010 [question] (2026-11-11, from architecture §12) Should `when:` also accept a simpler expression language that compiles to beets queries?
- B-011 [question] (2026-11-11, from architecture §12) Add a plugin exception to the AGPL so in-process plugins can use any license?

## Done
- B-009 [question] (2026-11-11, from architecture §12) Server ↔ worker IPC: multiprocessing pipes or a local socket? → F-WORKER (author: inherited pipes, 2026-11-11)
- B-006 [debt] (2026-11-11, from session) Run gitleaks in CI too, not only as a local pre-commit hook. → F-DEV-TOOLING
- B-013 [bug] (2026-11-11, from F-DEV-TOOLING) The Nix `pre-commit` (broken, see B-004) is back on PATH ahead of `/usr/bin/pre-commit`. The git hook itself uses `/usr/bin/python3` and works; manual `pre-commit run` picks the broken one. → removed from Nix profile by author, 2026-11-11
