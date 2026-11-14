# F-READ-ORDER: Request order for superseded read-only queries
Status: Draft
Milestone: M2

## Context
The profile's read-only worker (WORKER-11, architecture §8) answers script previews
(TAGSCRIPT-20) and library queries (TAG-GRID-2). Both take a `client` id, and a newer
request from the same client supersedes an older one still pending: the older one is
cancelled and answers `409 superseded` (TAGSCRIPT-21).

Today "newer" means "arrived at the server later". A browser sends each request on its own
connection, so requests can reach the server in a different order from the one they were
sent in. When the grid refetches several times in quick succession (once per stream event
after an apply or undo), TanStack Query abandons the older fetches, but they still reach the
server. If the newest fetch arrives first, a late abandoned one supersedes it, the browser
ignores the abandoned one's answer, and the page is left on stale data. This caused the
`TAG_GRID_15` flake (B-053). The frontend now retries a `409 superseded` answer
(`retrySuperseded` in `frontend/src/api/client.ts`), which hides the race but doesn't
remove it, and costs an extra round trip and a retry delay each time it happens.

This spec lets the client say which request is newer, so the server supersedes by send
order. It amends TAGSCRIPT-21 and TAG-GRID-2 (below) and comes from B-054.

## Behavior
- READ-ORDER-1: A request to the read-only worker that carries `client` MAY also carry
  `seq`, a non-negative integer that the client increases with every request it sends
  under that `client`. When a request has `seq`, "newer" in TAGSCRIPT-21 MUST mean a higher
  `seq`, not a later arrival.
- READ-ORDER-2: A request with `client` C and `seq` S MUST supersede every pending request
  of C with a lower `seq`: each is cancelled and answers `409 superseded`.
- READ-ORDER-3: A request with `client` C and `seq` S MUST answer `409 superseded` at once,
  without running, if C has a pending request with an equal or higher `seq`, or if the
  server has already received a higher `seq` from C (pending or not).
- READ-ORDER-4: The server MUST remember the highest `seq` received from each of at least
  the 256 most recently seen clients, and MAY forget older ones. A forgotten client is
  treated as new. The memory MUST be in process only and MUST reset when the server
  restarts.
- READ-ORDER-5: A request with `client` but no `seq` MUST keep the arrival-order rule of
  TAGSCRIPT-21, and a request without `client` is never superseded. Existing callers keep
  working unchanged.
- READ-ORDER-6: A request whose `client` has mixed requests with and without `seq` MUST
  compare only by the rule of the arriving request: one with `seq` supersedes pending ones
  with a lower `seq` or with no `seq`; one without `seq` supersedes all pending ones.
- READ-ORDER-7: The library grid (TAG-GRID-2) and the script editor's preview
  (TAGSCRIPT-22) MUST send `seq`, using one counter per page instance and a fresh `client`
  id per page instance (so a counter that restarts never reuses an id). Once they do, the
  B-053 retry (`retrySuperseded`) MUST be removed and both queries go back to
  `retry: false`.
- READ-ORDER-8: The Playwright test for the grid (TAG-GRID-15) MUST pass 40 of 40 runs
  with `--repeat-each=40` without the B-053 retry.

### S-TAGSCRIPT amendment this spec needs
- TAGSCRIPT-21 (amended): "A newer preview from the same client MUST supersede an older one
  still running" gains: "Which request is newer is set by `seq` when the request has one
  (F-READ-ORDER), otherwise by arrival."
- Preview request (Interfaces): `{script_text | script, query, scope?, limit?, client?,
  seq?}`.

### F-TAG-GRID amendment this spec needs
- TAG-GRID-2 (amended): `GET /api/profiles/{profile}/library` takes `seq` as a query
  parameter beside `client`; "Timeout and supersede behave as TAGSCRIPT-21" now includes
  F-READ-ORDER.

## Interfaces
- `PreviewRequest` (`rifferr.tagscript.preview`): new optional field
  `seq: int | None = Field(None, ge=0)`.
- `GET /api/profiles/{profile}/library`: new optional query parameter `seq` (integer, ≥ 0).
- `PreviewSupervisor.request(profile, request, *, timeout, client=None, seq=None)`; the
  pending-request record keeps `seq`, and the supervisor keeps a bounded
  `client → highest seq` map (READ-ORDER-4).
- `api.readonly.ask(..., client=None, seq=None)` passes `seq` through.
- Error answers are unchanged: `409 superseded` with the existing envelope. The message
  may say which `seq` superseded it.
- OpenAPI gains two optional fields, an additive change. Regenerate the TS client
  (`pnpm gen:api`).
- Frontend: a small hook or helper that returns `{ client, next(): number }` per page,
  used by `LibraryPage` and `ScriptPage`.

## Examples
Out-of-order arrival, client `grid-abc`:

| arrives | `seq` | outcome |
|---|---|---|
| 1st | 7 | runs |
| 2nd | 5 | `409 superseded` at once (READ-ORDER-3), never runs |
| 3rd | 6 | `409 superseded` at once (a higher `seq`, 7, was received) |
| 4th | 8 | supersedes 7 (`409`), runs and answers |

No `seq` (an older client, or `curl`): request A then B with `client=x`; B supersedes A,
as today (READ-ORDER-5).

## Extensibility
- Fixed: the ordering rule and the `409 superseded` answer. Any caller of the read-only
  worker that passes `client` may also pass `seq`; plugins don't take part.

## Security & safety
- `seq` and `client` come from the caller. A client can only supersede its own requests,
  as today. The per-client memory is bounded (READ-ORDER-4), so a caller that invents
  many client ids can't grow server memory without limit.
- Mutating endpoints are untouched; both routes stay read-only.

## Non-goals
- Ordering for anything other than read-only worker requests (runs, apply, undo jobs keep
  their own queue order).
- Cancelling a request when its HTTP connection closes. That would help too, but an
  abandoned request can still arrive after the newer one, so it doesn't fix the order.
  It could be a separate backlog entry.
- Changing the read-only worker's one-request-at-a-time model.

## Open questions
1. Is 256 remembered clients enough, or should it be a server setting? (Proposed: a
   constant, since each browser tab uses one or two client ids.)
2. Should `seq` be required whenever `client` is given in a later, breaking API version?
   (Proposed: no; READ-ORDER-5 keeps it optional.)

## Tasks
- [ ] `seq` on `PreviewRequest` and the library route; `ask` passes it through
- [ ] `PreviewSupervisor`: supersede by `seq` (READ-ORDER-2, -6), answer late arrivals at once (READ-ORDER-3), bounded highest-`seq` memory (READ-ORDER-4)
- [ ] supervisor and API tests per behavior, including out-of-order arrival and no `seq` (READ-ORDER-5)
- [ ] amend TAGSCRIPT-21, S-TAGSCRIPT Interfaces and TAG-GRID-2
- [ ] regenerate TS client; grid and editor send `seq`; remove `retrySuperseded` (READ-ORDER-7)
- [ ] repeated e2e run (READ-ORDER-8)
