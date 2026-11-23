import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-18T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

function conflict(over: Partial<S["MoveConflict"]> = {}): S["MoveConflict"] {
  return {
    id: "m1",
    profile: "main",
    run_id: RUN,
    step_id: "into",
    album_id: "album:1",
    label: "The Beatles - Abbey Road",
    state: "pending",
    conflicts: ["/music/Beatles, The/(1969) Abbey Road [FLAC]/01. Come Together.flac exists"],
    src_dir: "/inbox/Abbey Road",
    dst_dir: "/music/Beatles, The/(1969) Abbey Road [FLAC]",
    settings: { mode: "move", formats: null, directory: "/music", artifacts: [], stamp: null },
    created_at: T0,
    attempts: 0,
    decided_by: null,
    decided_at: null,
    changeset_id: null,
    ...over,
  };
}

function setup(c: S["MoveConflict"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        busy: false,
        kind: "move",
        id: c.id,
        profile: "main",
        run_id: RUN,
        workflow: "into-library",
        step_id: c.step_id,
        created_at: T0,
        move_conflict: c,
      } satisfies S["QueueItem"],
    ],
    "POST /api/move-conflicts/m1/decision": () => ({ job_id: "j1" }),
    ...extra,
  });
}

const bodies = (calls: Call[]) =>
  calls
    .filter((c) => c.method === "POST" && c.path === "/api/move-conflicts/m1/decision")
    .map((c) => c.body);

test("LIBRARY_MOVE_5a conflict lists folders and conflicts, then Retry", async () => {
  const { calls } = setup(conflict({ attempts: 2 }));
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Move conflict")).toBeInTheDocument();
  expect(within(card).getByTestId("move-folders")).toHaveTextContent(
    "/inbox/Abbey Road → /music/Beatles, The/(1969) Abbey Road [FLAC]",
  );
  expect(within(card).getByTestId("move-conflicts")).toHaveTextContent("Come Together.flac exists");
  expect(card).toHaveTextContent("Retried 2×, still conflicting.");
  fireEvent.click(within(card).getByRole("button", { name: "Retry" }));
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(card).not.toBeInTheDocument();
  });
  expect(bodies(calls)).toEqual([{ decision: "retry" }]);
});

test("LIBRARY_MOVE_5a Fail album, and errors show", async () => {
  const { calls } = setup(conflict({ src_dir: "", dst_dir: "" }), {
    "POST /api/move-conflicts/m1/decision": () => err(409, "busy", "a retry is running"),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("move-conflict");
  expect(within(card).queryByTestId("move-folders")).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "Fail album" }));
  expect(await within(card).findByText("a retry is running")).toBeInTheDocument();
  await waitFor(() => {
    expect(bodies(calls)).toEqual([{ decision: "fail" }]);
  });
});
