import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-21T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

const album = (id: string, label: string, items: number, missing: number): S["PruneAlbum"] => ({
  album_id: id,
  label,
  path: `/m/${label}`,
  items,
  missing,
});

const decision: S["PruneDecision"] = {
  id: "p1",
  profile: "main",
  run_id: RUN,
  step_id: "prune",
  state: "pending",
  albums: [album("1", "Band - Gone", 3, 3)],
  partial: [album("2", "Band - Half", 4, 1)],
  created_at: T0,
  decided_at: null,
  decided_by: null,
  note: null,
  result: null,
};

function setup() {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        kind: "prune",
        id: "p1",
        profile: "main",
        run_id: RUN,
        workflow: "wf",
        step_id: "prune",
        created_at: T0,
        prune_decision: decision,
      },
    ],
    "POST /api/prunes/p1/decision": () => ({ job_id: "j1" }),
  });
}

const bodies = (calls: Call[]) =>
  calls
    .filter((c) => c.method === "POST" && c.path === "/api/prunes/p1/decision")
    .map((c) => c.body);

test("LIBRARY_PRUNE_3 albums to drop and partly gone ones, then Drop", async () => {
  const { calls } = setup();
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Library entries without files")).toBeInTheDocument();
  expect(within(card).getByTestId("prune-album")).toHaveTextContent("3 files, all gone");
  expect(within(card).getByTestId("prune-partial")).toHaveTextContent("1 of 4 files gone");
  fireEvent.click(within(card).getByRole("button", { name: "Drop 1" }));
  await waitFor(() => {
    expect(card).not.toBeInTheDocument();
  });
  expect(bodies(calls)).toEqual([{ decision: "accept", note: null }]);
});

test("LIBRARY_PRUNE_3 keep all", async () => {
  const { calls } = setup();
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  fireEvent.click(within(card).getByRole("button", { name: "Keep all" }));
  await waitFor(() => {
    expect(bodies(calls)).toEqual([{ decision: "reject", note: null }]);
  });
});
