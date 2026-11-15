import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";
import { candidateImage } from "./CoverReviewView";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-14T12:00:00Z";
const RUN = "01CCCCCCCCCCCCCCCCCCCCCCCC";

function candidate(id: string, over: Partial<S["RankedCandidate"]> = {}): S["RankedCandidate"] {
  return {
    id,
    provider: "itunes",
    title: "Artist - Album",
    image_url: `https://is1-ssl.mzstatic.com/${id}.jpg`,
    path: null,
    thumb_url: null,
    width: 1200,
    height: 1200,
    match_confidence: 1,
    source_url: null,
    score: 1,
    ...over,
  };
}

function review(over: Partial<S["CoverReview"]> = {}): S["CoverReview"] {
  return {
    id: "v1",
    profile: "main",
    run_id: RUN,
    step_id: "art",
    album_id: "album-1",
    label: "Artist - Album",
    state: "pending",
    query: { artist: "Artist", album: "Album" },
    candidates: [
      candidate("c1"),
      candidate("c2", { provider: "deezer", width: null, height: null, score: 0.5 }),
    ],
    dropped: {},
    old_sha256: null,
    reason: "no cover scored 0.6 or more",
    created_at: T0,
    decided_by: null,
    decided_at: null,
    chosen: null,
    ...over,
  };
}

function setup(r: S["CoverReview"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        kind: "cover",
        id: r.id,
        profile: "main",
        run_id: r.run_id,
        workflow: "inbox",
        step_id: r.step_id,
        created_at: T0,
        cover_review: r,
      },
    ],
    "POST /api/covers/reviews/v1/decision": () => ({ job_id: "j1", sha256: "a".repeat(64) }),
    ...extra,
  });
}

const bodies = (calls: Call[]) =>
  calls
    .filter((c) => c.method === "POST" && c.path === "/api/covers/reviews/v1/decision")
    .map((c) => c.body);

test("COVERS_SEARCH_21 image links go through the server", () => {
  expect(candidateImage("main", "c1")).toBe("/api/covers/image/c1?profile=main&thumb=1");
  expect(candidateImage("main", "c1", false)).toBe("/api/covers/image/c1?profile=main");
});

test("COVERS_SEARCH_21 candidates are shown and one is chosen", async () => {
  const { calls } = setup(review());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Cover choice")).toBeInTheDocument();
  expect(within(card).getByTestId("cover-review")).toHaveTextContent("no cover scored 0.6 or more");
  const shown = within(card).getAllByTestId("cover-candidate");
  expect(shown).toHaveLength(2);
  expect(shown[0]).toHaveTextContent("1200×1200");
  expect(shown[1]).toHaveTextContent("size unknown");
  fireEvent.click(within(shown[0] as HTMLElement).getByRole("button", { name: "Use this cover" }));
  expect(await within(card).findByTestId("decision-sent")).toHaveTextContent("Setting the cover.");
  expect(bodies(calls)).toEqual([{ decision: "choose", candidate_id: "c1" }]);
});

test("COVERS_SEARCH_21 no cover, and errors show", async () => {
  const { calls } = setup(review({ candidates: [] }), {
    "POST /api/covers/reviews/v1/decision": () => err(409, "already_resolved", "already skipped"),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("cover-review");
  expect(within(card).getByTestId("no-candidates")).toBeInTheDocument();
  fireEvent.click(within(card).getByRole("button", { name: "No cover" }));
  expect(await within(card).findByText("already skipped")).toBeInTheDocument();
  await waitFor(() => {
    expect(bodies(calls)).toEqual([{ decision: "skip" }]);
  });
});
