import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { RunSummary, TriggerState, WorkflowDetail } from "../api/types";
import { mockApi, renderApp } from "../test-utils";

const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const SHA = "a".repeat(64);

afterEach(() => {
  vi.unstubAllGlobals();
});

function workflow(over: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    name: "new-music",
    path: "/cfg/profiles/main/workflows/new-music.yaml",
    sha256: SHA,
    valid: true,
    trusted: true,
    vars: [],
    missing_vars: [],
    yaml: "name: new-music\n",
    errors: [],
    ...over,
  };
}

function trigger(over: Partial<TriggerState>): TriggerState {
  return {
    workflow: "new-music",
    kind: "watch",
    state: "armed",
    detail: null,
    path: null,
    next_fire: null,
    last_fired: null,
    last_run_id: null,
    webhook_name: null,
    has_secret: null,
    ...over,
  };
}

test("TRIGGERS_4 workflow page lists its triggers with state", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/new-music": () => workflow(),
    "GET /api/profiles/main/triggers": () => [
      trigger({ kind: "watch", path: "/srv/inbox", last_run_id: "R1", last_fired: SHA_TIME }),
      trigger({ kind: "schedule", state: "disabled", detail: "untrusted" }),
      trigger({ workflow: "other", kind: "schedule" }),
    ],
  });
  renderApp("/workflows/new-music?profile=main");
  const panel = await screen.findByTestId("triggers");
  const watch = within(panel).getByTestId("trigger-watch");
  expect(within(watch).getByText("armed")).toBeInTheDocument();
  expect(within(watch).getByText("/srv/inbox")).toBeInTheDocument();
  expect(within(watch).getByRole("link")).toHaveAttribute("href", "/runs/R1?profile=main");
  const schedule = within(panel).getByTestId("trigger-schedule");
  expect(within(schedule).getByText("untrusted")).toBeInTheDocument();
  expect(within(panel).getAllByRole("row")).toHaveLength(2); // not the other workflow's
});
const SHA_TIME = "2026-11-13T04:00:00Z";

test("TRIGGERS_17 creating a webhook secret shows it once", async () => {
  let hasSecret = false;
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/new-music": () => workflow(),
    "GET /api/profiles/main/triggers": () => [
      trigger({ kind: "webhook", webhook_name: "dl", has_secret: hasSecret }),
    ],
    "POST /api/profiles/main/webhooks/dl/secret": () => {
      hasSecret = true;
      return { profile: "main", name: "dl", url_path: "/hooks/main/dl", secret: "s3cr3t" };
    },
  });
  renderApp("/workflows/new-music?profile=main");
  const panel = await screen.findByTestId("triggers");
  expect(within(panel).getByText("no secret yet")).toBeInTheDocument();
  fireEvent.click(within(panel).getByRole("button", { name: "Create secret" }));
  const shown = await screen.findByTestId("webhook-secret");
  expect(within(shown).getByText("s3cr3t")).toBeInTheDocument();
  expect(within(shown).getByText("POST /hooks/main/dl")).toBeInTheDocument();
  expect(await within(panel).findByRole("button", { name: "Replace secret" })).toBeInTheDocument();
  fireEvent.click(within(shown).getByRole("button", { name: "Done" }));
  await waitFor(() => {
    expect(screen.queryByText("s3cr3t")).not.toBeInTheDocument();
  });
  expect(calls.filter((c) => c.method === "POST")).toHaveLength(1);
});

test("TRIGGERS_7 start sends the input folder", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/new-music": () => workflow(),
    "POST /api/runs": () => ({ status: 201, body: { id: "R9" } }),
  });
  renderApp("/workflows/new-music?profile=main");
  const start = await screen.findByTestId("start");
  fireEvent.change(within(start).getByLabelText(/Input folder/), {
    target: { value: " /srv/inbox/A " },
  });
  fireEvent.click(within(start).getByRole("button", { name: "Start run" }));
  await waitFor(() => {
    expect(calls.some((c) => c.path === "/api/runs")).toBe(true);
  });
  const body = calls.find((c) => c.path === "/api/runs")?.body as { input?: string };
  expect(body.input).toBe("/srv/inbox/A");
});

test("TRIGGERS_24 runs list shows what started each run", async () => {
  const run = (over: Partial<RunSummary>): RunSummary => ({
    id: "R1",
    profile: "main",
    workflow: "new-music",
    workflow_hash: SHA,
    state: "succeeded",
    trusted: true,
    error: null,
    created_at: SHA_TIME,
    started_at: SHA_TIME,
    finished_at: SHA_TIME,
    trigger: "manual",
    input: null,
    ...over,
  });
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/runs": () => [
      run({ id: "R1", trigger: "watch", input: "/srv/inbox/Artist - Album" }),
      run({ id: "R2" }),
    ],
  });
  renderApp("/runs?profile=main");
  const table = await screen.findByTestId("runs");
  const [, first, second] = within(table).getAllByRole("row");
  if (first === undefined || second === undefined) throw new Error("expected two rows");
  expect(within(first).getByText("watch")).toBeInTheDocument();
  expect(within(first).getByText("Artist - Album")).toBeInTheDocument();
  expect(within(second).getByText("manual")).toBeInTheDocument();
});
