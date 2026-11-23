import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";
import { byClass, bytes } from "./CleanupDecisionView";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-13T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

function entry(path: string, cls: S["CleanupEntry"]["cls"], over: Partial<S["CleanupEntry"]> = {}) {
  return {
    path,
    kind: "file",
    cls,
    size: 2048,
    mtime_ns: null,
    reason: null,
    ...over,
  } satisfies S["CleanupEntry"];
}

function decision(over: Partial<S["CleanupDecision"]> = {}): S["CleanupDecision"] {
  return {
    id: "c1",
    profile: "main",
    run_id: RUN,
    step_id: "clean",
    path: "/in/Live",
    state: "pending",
    entries: [
      entry("info.nfo", "delete", { reason: "*.nfo" }),
      entry("Scans", "empty", { kind: "dir", size: null }),
      entry("notes.txt", "left"),
      entry("01.flac", "left", { reason: "importable" }),
    ],
    truncated: 0,
    totals: { delete: 1, empty: 1, left: 2 },
    options: {
      delete: ["*.nfo"],
      keep: [],
      empty_dirs: true,
      remove_root: false,
      trash: null,
      allow_audio: false,
    },
    created_at: T0,
    deadline: null,
    on_timeout: "keep",
    decided_at: null,
    decided_by: null,
    note: null,
    result: null,
    ...over,
  };
}

const cleanupItem = (d: S["CleanupDecision"]): S["QueueItem"] => ({
  busy: false,
  kind: "cleanup",
  id: d.id,
  profile: "main",
  run_id: d.run_id,
  workflow: "sweep",
  step_id: d.step_id,
  created_at: T0,
  cleanup_decision: d,
});

function setup(d: S["CleanupDecision"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [cleanupItem(d)],
    "POST /api/cleanups/c1/decision": () => ({ job_id: "j1" }),
    ...extra,
  });
}

const bodies = (calls: Call[]) =>
  calls
    .filter((c) => c.method === "POST" && c.path === "/api/cleanups/c1/decision")
    .map((c) => c.body);

test("FILES_CLEANUP_14 helpers", () => {
  expect(bytes(512)).toBe("512 B");
  expect(bytes(1536)).toBe("1.5 KB");
  expect(bytes(null)).toBe("");
  expect(byClass(decision().entries).map((g) => g.cls)).toEqual(["delete", "empty", "left"]);
});

test("FILES_CLEANUP_14 listing grouped by class, then Delete", async () => {
  const { calls } = setup(decision({ deadline: "2026-11-20T12:00:00Z" }));
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Folder cleanup")).toBeInTheDocument();
  expect(within(card).getByTestId("cleanup-path")).toHaveTextContent("/in/Live");
  const groups = within(card).getAllByTestId("cleanup-group");
  expect(groups.map((g) => g.dataset.cls)).toEqual(["delete", "empty", "left"]);
  expect(groups[0]).toHaveTextContent("info.nfo");
  expect(groups[0]).toHaveTextContent("2.0 KB");
  expect(groups[1]).toHaveTextContent("Scans/");
  expect(groups[2]).toHaveTextContent("importable");
  expect(within(card).getByTestId("deadline")).toHaveTextContent("keep everything automatically");
  fireEvent.change(within(card).getByLabelText("Note (optional)"), { target: { value: "ok" } });
  fireEvent.click(within(card).getByRole("button", { name: "Delete" }));
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(card).not.toBeInTheDocument();
  });
  expect(bodies(calls)).toEqual([{ decision: "delete", note: "ok" }]);
});

test("FILES_CLEANUP_14 keep everything, and errors show", async () => {
  const { calls } = setup(decision(), {
    "POST /api/cleanups/c1/decision": () => err(409, "already_resolved", "already kept"),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("cleanup-decision");
  fireEvent.click(within(card).getByRole("button", { name: "Keep everything" }));
  expect(await within(card).findByText("already kept")).toBeInTheDocument();
  await waitFor(() => {
    expect(bodies(calls)).toEqual([{ decision: "keep", note: null }]);
  });
});

test("FILES_CLEANUP_14 Delete is off when nothing would be deleted", async () => {
  setup(decision({ entries: [entry("notes.txt", "left")], truncated: 3 }));
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("cleanup-decision");
  expect(within(card).getByRole("button", { name: "Delete" })).toBeDisabled();
  expect(within(card).getByTestId("cleanup-truncated")).toHaveTextContent("3 more entries");
});
