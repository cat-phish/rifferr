import { act, fireEvent, screen, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { FakeEventSource, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];

function nth<T>(list: T[], n: number): T {
  const value = list[n];
  if (value === undefined) throw new Error(`no element ${String(n)}`);
  return value;
}
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-14T12:00:00Z";

function run(id: string, state: S["RunSummary"]["state"]): S["RunSummary"] {
  return {
    id,
    profile: "main",
    workflow: "foobar-style",
    workflow_hash: null,
    state,
    trusted: true,
    error: null,
    created_at: T0,
    started_at: T0,
    finished_at: null,
    trigger: "watch",
    input: "/music/inbox/A",
    called_hashes: {},
    lane_counts: state === "waiting_review" ? { waiting: 1, done: 2 } : {},
  };
}

let inbox: S["InboxFolder"][] = [];
const INBOX: S["InboxFolder"] = {
  workflow: "foobar-style",
  folder: "/music/inbox",
  trigger_state: "armed",
  detail: null,
  omitted: 0,
  entries: [
    { path: "<b>A</b>", state: "imported", changed_at: null, run_id: "R1" },
    { path: "B", state: "undone", changed_at: null, run_id: "R2" },
    { path: "C", state: "settling", changed_at: T0, run_id: null },
  ],
};

const IMPORTS: S["RecentImport"][] = [
  {
    run_id: "R1",
    workflow: "foobar-style",
    input: "/music/inbox/A",
    state: "succeeded",
    finished_at: T0,
    albums: [
      {
        id: "a1",
        label: "Band - Copy",
        items: 3,
        undoable: true,
        reason: null,
        deletes_files: true,
        files_at: "/music/lib/Copy",
      },
      {
        id: "a2",
        label: "Band - InPlace",
        items: 2,
        undoable: true,
        reason: null,
        deletes_files: false,
        files_at: "/music/inbox/A",
      },
    ],
  },
];

function setup() {
  inbox = [INBOX];
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/runs": () => [run("R3", "waiting_review"), run("R4", "succeeded")],
    "GET /api/review": () => [],
    "GET /api/profiles/main/inbox": () => inbox,
    "GET /api/profiles/main/imports": () => IMPORTS,
    "POST /api/runs": () => ({ status: 201, body: { id: "R9" } }),
    "POST /api/runs/R1/import-undo": () => ({ status: 202, body: { job_id: "J1" } }),
  });
}

test("DASHBOARD_14 the dashboard is the home screen with its four sections in order", async () => {
  setup();
  renderApp("/?profile=main");
  await screen.findByTestId("inbox-folder");
  const order = ["needs-review", "in-progress", "inbox", "recent-imports"].map((id) =>
    screen.getByTestId(id),
  );
  for (let n = 1; n < order.length; n++) {
    expect(
      nth(order, n - 1).compareDocumentPosition(nth(order, n)) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  }
  expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/runs?profile=main");
});

test("DASHBOARD_4 only runs in progress, with what they wait for", async () => {
  setup();
  renderApp("/?profile=main");
  const rows = await screen.findAllByTestId("active-run");
  expect(rows.map((r) => r.dataset.run)).toEqual(["R3"]);
  expect(rows[0]).toHaveTextContent("1 album waiting, 0 items to review");
});

test("DASHBOARD_1 DASHBOARD_3 DASHBOARD_16 inbox entries show their state as text and offer Import again", async () => {
  const { calls } = setup();
  renderApp("/?profile=main");
  const entries = await screen.findAllByTestId("inbox-entry");
  expect(entries[0]).toHaveTextContent("<b>A</b>");
  expect(entries.map((e) => e.dataset.state)).toEqual(["imported", "undone", "settling"]);
  expect(within(nth(entries, 0)).queryByRole("button")).toBeNull();
  fireEvent.click(within(nth(entries, 1)).getByRole("button", { name: "Import again" }));
  expect(await within(nth(entries, 1)).findByText("Started.")).toBeInTheDocument();
  const post = calls.find((c) => c.method === "POST" && c.path === "/api/runs");
  expect(post?.body).toEqual({
    profile: "main",
    workflow: "foobar-style",
    input: "/music/inbox/B",
  });
});

test("DASHBOARD_15 inbox refetches on inbox.changed", async () => {
  setup();
  renderApp("/?profile=main");
  await screen.findAllByTestId("inbox-entry");
  inbox = [{ ...INBOX, entries: [] }];
  act(() => {
    FakeEventSource.last("/api/events").emit("inbox.changed", {
      seq: 1,
      ts: T0,
      run_id: "profile:main",
      type: "inbox.changed",
      data: {},
    });
  });
  expect(await screen.findByText("Empty.")).toBeInTheDocument();
});

test("DASHBOARD_6 DASHBOARD_10 undo import confirms what happens to each album's files", async () => {
  const { calls } = setup();
  renderApp("/?profile=main");
  fireEvent.click(await screen.findByRole("button", { name: "Undo import" }));
  const lines = await screen.findAllByTestId("undo-album");
  expect(lines[0]).toHaveTextContent("Band - Copy: its files are deleted from the library folder");
  expect(lines[1]).toHaveTextContent("Band - InPlace: its files stay in /music/inbox/A");
  fireEvent.click(screen.getByRole("button", { name: "Remove 2" }));
  expect(await screen.findByTestId("undo-queued")).toBeInTheDocument();
  const post = calls.find((c) => c.path === "/api/runs/R1/import-undo");
  expect(post?.body).toEqual({ albums: ["a1", "a2"] });
});

test("DASHBOARD_5 needs review counts each kind and links the oldest items", async () => {
  const gates = Array.from({ length: 7 }, (_, n) => ({
    kind: "gate" as const,
    id: `g${String(n)}`,
    profile: "main",
    run_id: "R3",
    workflow: "foobar-style",
    step_id: "check",
    created_at: T0,
    changeset: null,
    state: null,
    gate: {
      id: `g${String(n)}`,
      profile: "main",
      run_id: "R3",
      step_id: "check",
      state: "pending" as const,
      albums: [
        {
          target: { scope: "album" as const, id: `a${String(n)}` },
          label: `Album ${String(n)}`,
          message: null,
          shown: {},
          changes: [],
          conflicted: [],
          imported_by_run: true,
        },
      ],
      on_reject: "stop" as const,
      on_timeout: null,
      created_at: T0,
      deadline: null,
      decided_at: null,
      decided_by: null,
      note: null,
      album_decisions: {},
    },
  }));
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/runs": () => [],
    "GET /api/review": () => gates,
    "GET /api/profiles/main/inbox": () => [],
    "GET /api/profiles/main/imports": () => [],
  });
  renderApp("/?profile=main");
  expect(await screen.findByTestId("review-kinds")).toHaveTextContent("7 review gates");
  const oldest = screen.getAllByTestId("review-oldest");
  expect(oldest).toHaveLength(5);
  expect(within(nth(oldest, 0)).getByRole("link", { name: "Album 0" })).toHaveAttribute(
    "href",
    "/review?profile=main&item=g0",
  );
});
