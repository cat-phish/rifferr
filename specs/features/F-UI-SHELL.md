# F-UI-SHELL: App shell, workflow list and live run log
Status: Draft
Milestone: M0

## Context
M0's done-when needs a workflow started from the UI with its log streaming
live (architecture §11). This spec covers the React app shell and the first
screens. The dashboard-as-queue (§2a) arrives in M1–M2; the M0 home screen is
a simple run list. The UI only uses generated API types (DEV-TOOLING-10) and
subscribes to SSE for progress (AGENTS.md conventions).

**Trust model (author, 2026-11-11):** the user is asked to trust a workflow
when it is *imported*, never when it is started. A workflow file that isn't
trusted yet arrived from elsewhere, so it counts as an import that still needs
review. Importing part of a workflow (pasting or importing steps, M1 editor)
will ask the same way (B-024). Workflows saved by Rifferr's own editor are
trusted at save time (SECURITY-10).

## Behavior
- UI-SHELL-1: The app MUST never see, show or store the token: sign-in is the server's `/auth/exchange` redirect (SECURITY-6), and the app uses only the resulting cookie. Any `401` from the API MUST show a "Signed out" screen that tells the user to open the sign-in link printed by `rifferr serve` (or the token file's path). It MUST NOT use localStorage or sessionStorage for anything auth-related.
- UI-SHELL-2: The shell MUST have a profile switcher, navigation (Runs, Workflows) and an area for the current page. The selected profile MUST be kept in the URL (`?profile=`); without one, the first loadable profile is selected. Profiles that fail to load are listed but disabled, with their error.
- UI-SHELL-3: The Workflows page MUST list the profile's workflows with validity and trust status, and show validation errors with their line and column.
- UI-SHELL-4: A workflow that isn't trusted MUST show as "Needs review". Review MUST show the workflow's YAML, its content hash and the full static safety report (also when it has no findings), and offer an explicit "Trust" action. That action sends the hash shown (`POST /api/workflows/{name}/trust`, API-14); a `409 hash_mismatch` MUST reload the review instead of trusting.
- UI-SHELL-5: Start MUST be offered only for workflows that are valid and trusted; for the others, Start is disabled with the reason ("Fix errors" / "Review first"). Starting MUST NOT ask about trust.
- UI-SHELL-6: Before starting, the UI MUST show a form for the workflow's declared vars (API-16): vars without a default that the profile doesn't set are required, the others show their default or profile value and may be overridden. Starting MUST call `POST /api/runs` with the vars and the hash the page showed (API-17), then navigate to the run page.
- UI-SHELL-7: The Run page MUST load the run (`GET /api/runs/{id}`) for its step list, then subscribe to `/api/runs/{id}/events` with `EventSource`. It renders steps with status and a live log, with stdout and stderr distinguished, ignores events whose `seq` it has already applied (the browser's reconnect sends `Last-Event-ID`), and closes the EventSource on `run.finished`.
- UI-SHELL-8: The Run page MUST offer Cancel while the run is queued or running (`POST /api/runs/{id}/cancel`).
- UI-SHELL-9: The Runs page MUST list recent runs of the selected profile with workflow, state, start time and duration, refetching when `/api/events` reports `run.started` or `run.finished` and after the UI starts a run.
- UI-SHELL-10: The log view MUST stay responsive with 10,000 log lines: virtualized rendering, with only the visible rows in the DOM.
- UI-SHELL-11: User-visible text MUST name beets concepts plainly (query, template, flexible attribute) and link to beets docs where they appear.
- UI-SHELL-12: Color scheme MUST follow the OS (light/dark).
- UI-SHELL-13: A Playwright test MUST run the two-step M0 workflow (`examples/workflows/two-step.yaml`: `beets.import` of a fixture album, then a `shell` step listing the imported files) end to end. `pnpm e2e` builds the frontend and starts `rifferr serve --no-browser` on a free port against a temp config, data and beets directory, signs in through `/auth/exchange` with the token file, reviews and trusts the workflow, starts it with its `inbox` var, and asserts the log shows output from both steps and the run succeeds.

### F-API and F-SECURITY additions this spec needs
- API-16 (new): `GET /api/workflows` and `GET /api/workflows/{name}` MUST include the workflow's declared vars (`name`, `default`, `description`, `profile_value`), and list vars without a default that the profile doesn't set as `missing_vars`. Those don't make a workflow invalid (they are supplied at start); every other validation error still does.
- API-17 (new): `POST /api/runs` MAY carry `sha256`. If it does and the file's current hash differs, the server MUST return `409 hash_mismatch` and queue nothing, so the UI starts exactly what it showed.
- API-13 (amend): the static frontend MUST fall back to `index.html` for `GET` paths outside `/api`, `/auth`, `/healthz` and `/openapi.json`, so deep links like `/runs/<id>` survive a reload.
- SECURITY-7 (amend, not a loosening): an unauthenticated `GET` that accepts `text/html` MUST get a small HTML "Signed out" page with the same `401` status. It explains how to sign in and carries no app data; API clients keep the JSON envelope.

## Interfaces
- Dependencies (author, 2026-11-11; all MIT unless noted): `@mantine/core` + `@mantine/hooks` 9.x (no PostCSS preset), `react-router` 8.x (library mode), `@tanstack/react-query` 5.x, `@tanstack/react-virtual` 3.x, `openapi-fetch` 0.17 over the generated `schema.d.ts`; dev: `@playwright/test` 1.x (Apache-2.0).
- Routes: `/` and `/runs` (Runs; in M2 the queue dashboard takes `/`, and `/runs` stays), `/runs/:id`, `/workflows`, `/workflows/:name` (detail + review + start). All take `?profile=`.
- `src/api/client.ts`: one `openapi-fetch` client (same origin, `credentials: "same-origin"`, `Content-Type: application/json`); a 401 from it moves the app to the Signed out screen.
- `src/hooks/useEventStream.ts`: `useEventStream(url, { onEvent, closeOn })`, deduping by `seq`.
- `pnpm e2e`: `playwright test` with a global setup that prepares the temp profile and starts the server; the browsers come from `pnpm exec playwright install chromium` (one-time, needs network; the test itself needs none).

## Examples
Open Workflows → "two-step" shows "Needs review" → Review shows YAML and two
safety findings (`command`, `shell_string`) → Trust → Start, with the form
asking for `inbox` → Run page shows `import` running, then `imported: A - B`,
then `list-files` with `ls` output, then "Succeeded" with duration.

## Extensibility
None in M0. Step-specific result views come with their step specs.

## Security & safety
- No token in storage or in visible URLs after the exchange (UI-SHELL-1).
- The safety report is always shown before trust is granted, and trust is bound to the hash shown (UI-SHELL-4, API-14).
- A run starts only from content the page showed (API-17).

## Non-goals
- Workflow editor, tag grid, review queue, dashboard (M1+).
- Theming beyond light/dark following the OS.
- Showing runs before they start in the Runs list when another client (CLI) queued them; they appear on `run.started` (B-025).

## Open questions
- ~~Component library?~~ Resolved 2026-11-11 (author): Mantine (B-008).
- ~~Router?~~ Resolved 2026-11-11 (author): React Router (8.x, the current major; library mode).
- ~~Trust prompt at start?~~ Resolved 2026-11-11 (author): trust is asked at import, always; never at start (UI-SHELL-4, -5).
- M0 home screen vs. future dashboard: proposed `/` = Runs now, with `/runs` as a stable alias, so the M2 dashboard can take `/` without breaking links. Confirm.

## Tasks
- [ ] F-API additions: API-16, API-17, SPA fallback; F-SECURITY HTML 401 page
- [ ] dependencies, Mantine provider (auto color scheme), router, API client
- [ ] app shell + profile switcher + Signed out screen
- [ ] workflows page, review + trust, vars form + start
- [ ] run page with SSE log (virtualized) + cancel
- [ ] runs list with live updates
- [ ] `examples/workflows/two-step.yaml`
- [ ] Playwright e2e (`pnpm e2e`)
