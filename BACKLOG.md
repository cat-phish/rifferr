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

## Issues
- B-004 [bug] (2026-11-11, from session) Nix `pre-commit` package fails to build Python hook environments (`ModuleNotFoundError: No module named 'packaging'` in its virtualenv). Use the distro package or pipx; mention in contributor docs.

## Tech debt
- B-005 [debt] (2026-11-11, from session) No LICENSE file yet; add the AGPL-3.0-or-later text.
- B-006 [debt] (2026-11-11, from session) Run gitleaks in CI too, not only as a local pre-commit hook (lands with F-DEV-TOOLING).
- B-007 [debt] (2026-11-11, from session) Decide a cadence for `pre-commit autoupdate` (or let Renovate/Dependabot do it).

## Questions
- B-008 [question] (2026-11-11, from architecture §12) Which UI component library (shadcn/ui, Mantine, …)? Needed for F-UI-SHELL.
- B-009 [question] (2026-11-11, from architecture §12) Server ↔ worker IPC: multiprocessing pipes or a local socket? Needed for F-WORKER.
- B-010 [question] (2026-11-11, from architecture §12) Should `when:` also accept a simpler expression language that compiles to beets queries?
- B-011 [question] (2026-11-11, from architecture §12) Add a plugin exception to the AGPL so in-process plugins can use any license?

## Done
