import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { err, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];

function nth<T>(list: T[], n: number): T {
  const value = list[n];
  if (value === undefined) throw new Error(`no element ${String(n)}`);
  return value;
}
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-13T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

function album(id: string, label: string): S["GateAlbum"] {
  return {
    target: { scope: "album", id },
    label,
    message: null,
    shown: {},
    changes: [],
    conflicted: [],
    imported_by_run: false,
  };
}

function gate(id: string, over: Partial<S["ReviewGate"]> = {}): S["ReviewGate"] {
  return {
    edit: false,
    edits: [],
    id,
    profile: "main",
    run_id: RUN,
    step_id: "check",
    state: "pending",
    albums: [album("a1", "Band - One")],
    on_reject: "stop",
    on_timeout: null,
    created_at: T0,
    deadline: null,
    decided_at: null,
    decided_by: null,
    note: null,
    album_decisions: {},
    ...over,
  };
}

const item = (g: S["ReviewGate"]): S["QueueItem"] => ({
  busy: false,
  kind: "gate",
  id: g.id,
  profile: "main",
  run_id: RUN,
  workflow: "tagging",
  step_id: "check",
  created_at: T0,
  changeset: null,
  state: null,
  gate: g,
});

test("DASHBOARD_12 a gate of several albums is decided one album at a time", async () => {
  const two = gate("g1", { albums: [album("a1", "Band - One"), album("a2", "Band - Two")] });
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [item(two)],
    "POST /api/reviews/g1/decision": () => ({ ...two, album_decisions: { a2: "rejected" } }),
  });
  renderApp("/review?profile=main");
  const actions = await screen.findAllByTestId("album-actions");
  expect(actions).toHaveLength(2);
  fireEvent.click(within(nth(actions, 1)).getByRole("button", { name: "Reject this album" }));
  expect(await screen.findByTestId("album-decided")).toHaveTextContent("rejected");
  const post = calls.find((c) => c.method === "POST");
  expect(post?.body).toEqual({ decision: "reject", note: null, albums: ["a2"] });
  expect(screen.getByRole("button", { name: "Continue the rest" })).toBeInTheDocument();
  expect(screen.getAllByTestId("album-actions")).toHaveLength(1);
});

test("DASHBOARD_12 a gate of one album keeps the plain buttons", async () => {
  mockApi({ "GET /api/profiles": () => PROFILES, "GET /api/review": () => [item(gate("g1"))] });
  renderApp("/review?profile=main");
  expect(await screen.findByRole("button", { name: "Continue" })).toBeInTheDocument();
  expect(screen.queryByTestId("album-actions")).toBeNull();
});

test("DASHBOARD_13 several items of one kind are decided together, failures reported", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [item(gate("g1")), item(gate("g2")), item(gate("g3"))],
    "POST /api/reviews/g1/decision": () => gate("g1", { state: "continued" }),
    "POST /api/reviews/g2/decision": () =>
      err(409, "already_resolved", "gate g2 is already rejected"),
  });
  renderApp("/review?profile=main");
  const boxes = await screen.findAllByTestId("select-item");
  fireEvent.click(nth(boxes, 0));
  fireEvent.click(nth(boxes, 1));
  const bar = await screen.findByTestId("bulk-bar");
  expect(bar).toHaveTextContent("2 items selected");
  fireEvent.click(within(bar).getByRole("button", { name: "Continue selected" }));
  const failures = await screen.findByTestId("bulk-failures");
  expect(failures).toHaveTextContent("1 item could not be decided");
  expect(failures).toHaveTextContent("gate g2 is already rejected");
  const posts = calls.filter((c) => c.method === "POST").map((c) => c.path);
  expect(posts).toEqual(["/api/reviews/g1/decision", "/api/reviews/g2/decision"]);
  await waitFor(() => {
    expect(screen.queryByTestId("bulk-bar")).toBeNull();
  });
});

test("REVIEW_QUEUE_21 select all picks every item of the step, and again clears them", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [item(gate("g1")), item(gate("g2")), item(gate("g3"))],
  });
  renderApp("/review?profile=main");
  const all = await screen.findByLabelText("Select all 3");
  expect(screen.getAllByLabelText("Select for bulk action")).toHaveLength(3);
  fireEvent.click(all);
  expect(await screen.findByTestId("bulk-bar")).toHaveTextContent("3 items selected");
  fireEvent.click(all);
  await waitFor(() => {
    expect(screen.queryByTestId("bulk-bar")).toBeNull();
  });
});
