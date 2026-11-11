# F-SECURITY: Request checks, local token, remote auth and workflow trust (M0 slice)
Status: Draft
Milestone: M0

## Context
Rifferr can run shell commands, so the threat is who can trigger it: any web
page in the user's browser (cross-site requests, DNS rebinding) and other
local users (architecture §8 "Security"; AGENTS.md non-negotiable 5). These
checks exist before the first mutating endpoint ships. M0 includes the
request checks, the local token, the non-localhost refusal and a minimal
workflow trust gate; the full static safety report UI comes later.

## Behavior
- SECURITY-1: The server MUST bind to `127.0.0.1` by default.
- SECURITY-2: Requests whose `Host` header is not in the allowlist (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, plus configured names) MUST get 421/400 and no other processing. This MUST stay on with `--no-auth`.
- SECURITY-3: Requests carrying an `Origin` header that is not the app's own origin MUST be rejected with 403; mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) without an `Origin` or matching `Sec-Fetch-Site: same-origin` MUST also be rejected, except webhook routes (M2) and requests with a valid bearer token from the CLI.
- SECURITY-4: Mutating endpoints MUST accept only `Content-Type: application/json` (415 otherwise).
- SECURITY-5: The server MUST send no CORS headers.
- SECURITY-6: At launch, the server MUST generate a random local token (≥ 32 bytes, `secrets.token_urlsafe`). `rifferr serve` opens `http://127.0.0.1:<port>/?token=…`; the app exchanges it for an `HttpOnly`, `SameSite=Strict` cookie and redirects to a URL without the token.
- SECURITY-7: Every API route except the token exchange and health check MUST require the cookie or `Authorization: Bearer <token>` (for the CLI), unless `--no-auth` is given. Host/Origin/JSON checks stay on with `--no-auth`.
- SECURITY-8: The token MUST be written to a file readable only by the user (mode 0600, user runtime dir) so `rifferr run` can talk to a running server, and MUST never be logged.
- SECURITY-9: Binding to any address other than loopback MUST refuse to start unless real auth is configured (username + password, or trusted reverse-proxy header with a configured proxy address).
- SECURITY-10: Workflows MUST be trusted by content hash (SHA-256 of the file bytes) before any `shell` step (and later `python` actions) runs; workflows created in this install through the UI or CLI are trusted at save time; any edit changes the hash and resets trust.
- SECURITY-11: In M0, `rifferr validate` and the API's validate route MUST produce a static safety report listing every `shell` step's command, `shell: true` usage, network tools (`curl`, `wget`, `ssh`, `nc`, …) and destructive commands (`rm`, `mv`, `chmod`, `chown`, `sudo`) found in argv.
- SECURITY-12: Trust MUST be granted only by an explicit user action (`rifferr trust <file>` or a UI confirm), never automatically for files that arrived from elsewhere, and never by AI review.

## Interfaces
- Middleware stack (outermost first): Host allowlist → Origin/fetch-site check → content-type check → auth.
- Routes: `GET /auth/exchange?token=…` (sets cookie, 303 to `/`), `GET /healthz` (no auth).
- Token file: `$XDG_RUNTIME_DIR/rifferr/<port>.token` (fallback: config dir, mode 0600).
- Trust store: app DB table `trusted_files(sha256, path_hint, trusted_at, trusted_by)`.

## Examples
- `curl -H 'Host: evil.example' http://127.0.0.1:8765/api/runs` → 400.
- Browser page on another origin POSTs JSON to `/api/runs` → 403.
- `rifferr serve --host 0.0.0.0` without auth config → exits non-zero: "binding to 0.0.0.0 requires authentication; see docs".
- Copy `examples/workflows/two-step.yaml` into the workflows dir, run it → shell step fails "workflow not trusted"; `rifferr trust` it → runs.

## Extensibility
Auth backends for remote mode (password, proxy header) are fixed in M0. The safety report's rule list is data, so it can grow without code changes to callers.

## Security & safety
This spec is the security baseline; loosening any rule needs the author's explicit OK (AGENTS.md).

## Non-goals
- Safety report UI and AI-assisted review (later).
- User accounts beyond a single remote login.
- Webhook secrets (F-TRIGGERS, M2).

## Open questions
- Remote mode in M0, or only the refusal (SECURITY-9) with real auth arriving in F-PACKAGING (M5, Docker)?
- Should files under the repo's `examples/` be shipped as untrusted, so users see the safety report on first run?
- Which status code for Host mismatch: 400 or 421?

## Tasks
- [ ] host allowlist middleware
- [ ] origin / fetch-site middleware
- [ ] JSON-only middleware
- [ ] token generation, exchange, cookie, token file
- [ ] non-localhost refusal
- [ ] trust store + hash check in shell step
- [ ] static safety report
