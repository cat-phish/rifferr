# F-SECURITY: Request checks, local token, remote auth and workflow trust (M0 slice)
Status: Implemented
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
- SECURITY-2: Requests whose `Host` header is not in the allowlist (`127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`, plus configured names) MUST get 400 and no other processing. This MUST stay on with `--no-auth`. `rifferr serve --allowed-host NAME[:PORT]` (`ApiSettings.extra_hosts`, CLI-18) supplies the configured names; both `http://` and `https://` origins of each are allowed. Supported proxy setup: pass `Host` and `Origin` through unchanged, serve https, and don't log the `/auth/exchange` query. Rewriting Host or Origin is not supported.
- SECURITY-3: Requests carrying an `Origin` header that is not the app's own origin MUST be rejected with 403; mutating requests (`POST`, `PUT`, `PATCH`, `DELETE`) without an `Origin` or matching `Sec-Fetch-Site: same-origin` MUST also be rejected, except webhook routes (M2) and requests with a valid bearer token from the CLI. For `/api/*` and `/openapi.json`, a request with `Sec-Fetch-Site` other than `same-origin` or `none` MUST be refused 403 `cross_origin`, for every method; a missing header is still allowed (CLI, scripts). Page navigations and `/auth/*` are unaffected.
- SECURITY-4: Mutating endpoints MUST accept only `Content-Type: application/json` (415 otherwise).
- SECURITY-5: The server MUST send no CORS headers.
- SECURITY-6: At launch, the server MUST generate a random local token (≥ 32 bytes, `secrets.token_urlsafe`). `rifferr serve` opens `http://127.0.0.1:<port>/auth/exchange?token=…`, where `token` is a one-time sign-in code (32 random bytes, valid 15 minutes, removed on first exchange; printed and opened links share one code), not the local token; the app exchanges it for an `HttpOnly`, `SameSite=Strict` cookie named `rifferr_token_<port>` (browsers share localhost cookies across ports) and redirects (303) to a URL without the token. The exchange response MUST send `Cache-Control: no-store`. The cookie holds a random session id kept in server memory (it ends at restart), never the local token. A session id is not accepted as `Authorization: Bearer` and does not lift the Origin rule. The server MUST read every `rifferr_token_<port>` value in `Cookie` (hand-parsed, so a foreign or malformed cookie can't hide it). A cookie holding the local token itself is still accepted. The cookie gets `Secure` when the request is https or its `Host` is a configured extra host, never on plain-http loopback. `/auth/exchange` still accepts the local token as a fallback; `POST /auth/sign-in-link` (Bearer local token only) mints a new one-time code for `rifferr sign-in` (CLI-16).
- SECURITY-7: Every API route except the token exchange and health check MUST require the cookie or `Authorization: Bearer <token>` (for the CLI), unless `--no-auth` is given. Tokens MUST be compared in constant time (`hmac.compare_digest`). Host/Origin/JSON checks stay on with `--no-auth`. An unauthenticated `GET` whose `Accept` includes `text/html` MUST get a small HTML "Signed out" page (no app data) with the same `401` status, instead of the JSON envelope (added for F-UI-SHELL; not a loosening).
- SECURITY-8: The token MUST be written to a file readable only by the user (mode 0600, user runtime dir) so `rifferr run` can talk to a running server, and MUST never be logged.
- SECURITY-9: Binding to any address other than loopback MUST refuse to start unless real auth is configured (username + password, or trusted reverse-proxy header with a configured proxy address). In M0 no real auth exists yet, so any non-loopback bind MUST refuse to start with a message pointing at the docs. `--no-auth` together with any `--allowed-host` MUST refuse to start (exit 2).
- SECURITY-10: Workflows MUST be trusted by content hash (SHA-256 of the file bytes) before any `shell` step (and later `python` actions) runs; workflows created in this install through the UI or CLI are trusted at save time; any edit changes the hash and resets trust. The server MUST check trust when a run is submitted, against the hash of the workflow text stored with the job (F-WORKER), and pass the result as the run's `trusted` flag; the worker runs that same text. Step types from installed plugins are outside this gate: installing a plugin is what trusts it. A workflow file is trusted in a profile when its SHA-256 is recorded as trusted for that profile or for every profile (`profile = ''`). Trust MUST NOT be granted for a workflow that fails validation.
- SECURITY-11: In M0, `rifferr validate` and the API's validate route MUST produce a static safety report listing every `shell` step's command, `shell: true` usage, network tools (`curl`, `wget`, `ssh`, `nc`, …), destructive commands (`rm`, `mv`, `chmod`, `chown`, `sudo`) found in argv, every secret a step exposes (`with.secrets`), and local scripts the workflow runs (argv paths starting with `./`, `../`, `~` or `/` that aren't system binaries), with a warning that trust covers the workflow text only (B-020). `beets.import` MUST be listed with `move: true`, `write: true` and a destructive `duplicate_action` as `destructive`. `<interpreter> -c '<code>'` MUST give a `shell_string` finding, and the string is scanned for network and destructive tools. A program word containing `{{` or `$` MUST give a `local_script` finding ("program comes from a variable"). `tag_field` findings are specified in SHELL-20, `auto_apply` in TAGSCRIPT-18, and `file_move` in LIBRARY-MOVE-6a.
- SECURITY-12: Trust MUST be granted only by an explicit user action (`rifferr trust <file>` or a UI confirm), never automatically for files that arrived from elsewhere (including the repo's `examples/`, which ship untrusted), and never by AI review.
- SECURITY-13: Every response MUST carry `Content-Security-Policy` with `frame-ancestors 'none'`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`, so pages like the trust confirmation can't be framed (clickjacking) and the token can't leak through `Referer`. The fixed policy is `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`. A route's own CSP (images) only gets `frame-ancestors` appended. Vite does not apply it in `--dev`.
- SECURITY-14: A profile's beets config that runs code (`pluginpath` non-empty, or plugins `inline` or `hook`; `config.yaml` plus its `include:` files, read as plain YAML) MUST be loaded by the worker only if its SHA-256 over those files (prefixed `rifferr beets-config v1`) is trusted for that profile in `trusted_files`. Otherwise the worker fails to start with a message naming the keys and `rifferr trust-config PROFILE`. Trust is granted only by `rifferr trust-config` (CLI-17) or `POST /api/profiles/{p}/beets-config/trust` (API-19), under SECURITY-12; any change to those files changes the hash and resets it (BEETS-IMPORT-18).

## Interfaces
- Middleware stack (outermost first): security headers → Host allowlist → Origin/fetch-site check → content-type check → auth. Pure ASGI (`rifferr.api.security`), no framework dependency.
- Routes: `GET /auth/exchange?token=…` (sets cookie, 303 to `/`), `GET /healthz` (no auth).
- Token file: `$XDG_RUNTIME_DIR/rifferr/<port>.token` (fallback: config dir, mode 0600).
- Trust store: app DB table `trusted_files(sha256, profile, path_hint, trusted_at, trusted_by)`, primary key `(sha256, profile)`; `profile = ''` means every profile. Migration `015_trust_profile.sql` moves existing rows to `''`, so no stored trust is lost or narrowed. Beets-config hashes (SECURITY-14) live in the same table, domain-separated by their prefix.

As implemented:
- `rifferr.api.security`: `SecurityConfig(port, token, no_auth, extra_hosts, exempt_prefixes)`,
  middleware `SecurityHeaders`, `HostAllowlist`, `OriginCheck`, `JsonOnly`, `TokenAuth`, and
  `secure(app, config)` to stack them; `generate_token`, `write_token_file` /
  `read_token_file` / `remove_token_file`, `check_bind`, `BindRefusedError`. The token
  exchange (`/auth/exchange`) is served by `TokenAuth` itself. Rejections are JSON
  `{"error": …}` with `Cache-Control: no-store`; 401 for missing auth. WebSocket
  connections that fail the Host or auth check are closed with code 1008.
- Extra hosts are accepted as `http://` and `https://` origins (reverse proxy).
- Token file fallback without `XDG_RUNTIME_DIR`: `<config dir>/run/<port>.token`.
- `rifferr.trust`: `workflow_hash`, `read_workflow_file` (bytes, strict UTF-8, no newline
  translation), `trust_file`. `AppDB.trust` / `AppDB.is_trusted` over `trusted_files`.
- `WorkerSupervisor.submit_run` no longer takes `trusted`: it looks up the hash of the
  submitted text (SECURITY-10).
- `rifferr.core.workflow.safety_report(wf)` → `SafetyReport(findings)`; finding kinds
  `command`, `shell_string`, `network_tool`, `destructive`, `secret`, `local_script`. Local
  scripts are reported only where a program runs (first word, after `;` `&&` `||` `|` `&`,
  or the script an interpreter like `python3`/`sh` runs). Rule lists are module constants.
- Wiring into `rifferr validate`, `rifferr trust` and the API arrives with F-CLI / F-API.
- The signed-out page (SECURITY-7) is `SIGNED_OUT_PAGE` in `rifferr.api.security`, sent for
  unauthenticated `GET`s whose `Accept` includes `text/html`.
- Extended by F-TRIGGERS: `/hooks/` routes are exempt from the Origin and JSON checks (SECURITY-3) and from the local token (`SecurityConfig.own_auth_prefixes`); they check their own per-webhook secrets (TRIGGERS-17) and stay behind the Host allowlist. Triggers arm only for trusted workflows (TRIGGERS-2).
- In `--dev` the Vite proxy rewrites `Host`, so Vite's own `allowedHosts` check (default) is the DNS-rebinding defence there, not SECURITY-2 (pinned by a test, SEC-48).

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
- The DNS-rebinding defence in `--dev` is Vite's `allowedHosts` check; the Rifferr Host allowlist sees the rewritten Host (see Interfaces).

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
- (2026-11-18, audit SEC-63) Legacy `trusted_files` rows migrated to `profile = ''` (every profile). Alternative for the author: bind them to the profile parsed from `path_hint` (`<config>/profiles/<name>/workflows/...`), falling back to `''`.
- (2026-11-18, audit SEC-63) Trust revoke is missing: `AppDB.untrust`, `rifferr untrust FILE`, a UI action and a listing of trusted entries. A suspended run that is requeued should re-check trust instead of replaying the payload `trusted` flag.
- (2026-11-18, audit SEC-64) Should trust on a non-loopback deployment need fresh proof (password or CLI only)? Should every trust emit a notification or event?
- (2026-11-18, audit SEC-42/43) Drop the `/auth/exchange` local-token fallback once e2e and docs use `rifferr sign-in`?
- (2026-11-18, audit SEC-46) Must a proxied server (any `--allowed-host`) have real auth? The local token is otherwise the only LAN auth. Not enforced beyond SECURITY-9's `--no-auth` refusal.
- (2026-11-18, audit SEC-62) The UI has no button for beets-config trust yet. The `convert`, `badfiles`, `play` and `replaygain` command backends are not counted as code (BEETS-IMPORT-18).
- (2026-11-18, audit SEC-41..47) `/api/events` is global across profiles; see F-API open questions.

## Tasks
- [x] security headers middleware
- [x] host allowlist middleware
- [x] origin / fetch-site middleware
- [x] JSON-only middleware
- [x] token generation, exchange, cookie, token file
- [x] non-localhost refusal
- [x] trust store + hash check in shell step
- [x] static safety report
