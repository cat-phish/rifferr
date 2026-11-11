# F-UI-SHELL: App shell, workflow list and live run log
Status: Draft
Milestone: M0

## Context
M0's done-when needs a workflow started from the UI with its log streaming
live (architecture §11). This spec covers the React app shell and the first
screens. The dashboard-as-queue (§2a) arrives in M1–M2; the M0 home screen is
a simple run list. The UI only uses generated API types (DEV-TOOLING-10) and
subscribes to SSE for progress (AGENTS.md conventions).

## Behavior
- UI-SHELL-1: The app MUST complete the token exchange (SECURITY-6) on first load and never show or store the token itself (no localStorage).
- UI-SHELL-2: The shell MUST have a profile switcher, navigation (Runs, Workflows) and an area for the current page; the selected profile is kept in the URL.
- UI-SHELL-3: The Workflows page MUST list the profile's workflows with validity and trust status, and show validation errors with their line numbers.
- UI-SHELL-4: A workflow whose declared vars lack values MUST show a form for them before starting; starting MUST call `POST /api/runs` and navigate to the run page.
- UI-SHELL-5: Starting an untrusted workflow MUST show the static safety report and require an explicit "Trust and run" confirmation (SECURITY-12).
- UI-SHELL-6: The Run page MUST subscribe to `/api/runs/{id}/events` and render steps with status and a live log, with stdout/stderr distinguished; on reconnect it MUST resume from the last seen event id without duplicating lines.
- UI-SHELL-7: The Run page MUST offer Cancel while the run is active.
- UI-SHELL-8: The Runs page MUST list recent runs with state and duration, updating live from `/api/events`.
- UI-SHELL-9: The log view MUST stay responsive with 10,000 log lines (virtualized rendering).
- UI-SHELL-10: User-visible text MUST name beets concepts plainly (query, template, flexible attribute) and link to beets docs where they appear.
- UI-SHELL-11: A Playwright test MUST run the two-step M0 workflow end to end against a temp profile (backend started by `pnpm e2e`) and assert the log shows output from both steps.

## Interfaces
Routes: `/` (Runs), `/workflows`, `/runs/:id`, each with `?profile=`.
State: TanStack Query for REST; one small SSE hook (`useEventStream(url)`) with `Last-Event-ID` resume.

## Examples
Start "two-step" → Run page shows `import` running, then `imported: A - B`,
then `list-files` with `ls` output, then "Succeeded" with duration.

## Extensibility
None in M0. Step-specific result views come with their step specs.

## Security & safety
- No token in localStorage or visible URLs after exchange (UI-SHELL-1).
- Safety report shown before any untrusted shell step runs (UI-SHELL-5).

## Non-goals
- Workflow editor, tag grid, review queue, dashboard (M1+).
- Theming beyond light/dark following the OS.

## Open questions
- Component library: shadcn/ui, Mantine, or none for M0 (BACKLOG B-008)? Needs a license check and approval.
- Router: React Router or TanStack Router?
- Should the M0 home screen already be shaped like the future queue dashboard, to avoid a redesign in M2?

## Tasks
- [ ] app shell + routing + profile switcher
- [ ] token exchange handling
- [ ] workflows page with vars form and trust confirm
- [ ] run page with SSE log + cancel
- [ ] runs list with live updates
- [ ] Playwright e2e
