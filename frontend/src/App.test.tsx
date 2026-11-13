import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { expect, test, vi } from "vitest";
import type { RunDetail, WorkflowDetail } from "./api/types";
import { BeetsTerm } from "./components/BeetsTerm";
import { LogView } from "./components/LogView";
import { BEETS_DOCS } from "./lib/beetsDocs";
import type { LogLine } from "./lib/runState";
import { Providers, newQueryClient } from "./providers";
import { FakeEventSource, currentLocation, err, media, mockApi, renderApp } from "./test-utils";

const PROFILES = [
  { name: "broken", backend: null, vars: [], secrets: [], error: "bad yaml" },
  { name: "main", backend: "beets", vars: [], secrets: [], error: null },
  { name: "other", backend: "beets", vars: [], secrets: [], error: null },
];
const SHA = "a".repeat(64);

function workflow(over: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    name: "two-step",
    path: "/cfg/profiles/main/workflows/two-step.yaml",
    sha256: SHA,
    valid: true,
    trusted: true,
    vars: [
      { name: "inbox", default: null, description: "new albums", profile_value: null },
      { name: "mode", default: "copy", description: null, profile_value: null },
    ],
    missing_vars: ["inbox"],
    yaml: "name: two-step\nsteps: []\n",
    errors: [],
    ...over,
  };
}

function runDetail(over: Partial<RunDetail> = {}): RunDetail {
  return {
    id: "R1",
    profile: "main",
    workflow: "two-step",
    workflow_hash: SHA,
    state: "running",
    trusted: true,
    error: null,
    created_at: "2026-11-11T12:00:00Z",
    started_at: "2026-11-11T12:00:01Z",
    finished_at: null,
    steps: [
      { id: "import", uses: "beets.import", status: "pending", outputs: {} },
      { id: "list", uses: "shell", status: "pending", outputs: {} },
    ],
    undoable: 0,
    ...over,
  };
}

const ev = (seq: number, type: string, step: string | null, data: object) => ({
  seq,
  ts: "2026-11-11T12:00:02Z",
  run_id: "R1",
  type,
  step_id: step,
  data,
});

// -- UI-SHELL-1 ---------------------------------------------------------------------------------

test("UI_SHELL_1 a 401 shows Signed out and nothing is stored", async () => {
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  mockApi({ "GET /api/profiles": () => err(401, "unauthenticated") });
  renderApp("/");
  expect(await screen.findByRole("heading", { name: "Signed out" })).toBeInTheDocument();
  expect(screen.getByText(/rifferr serve/)).toBeInTheDocument();
  expect(setItem).not.toHaveBeenCalled();
});

// -- UI-SHELL-2 ---------------------------------------------------------------------------------

test("UI_SHELL_2 profile switcher defaults to the first loadable profile and keeps it in the URL", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/runs": () => [],
  });
  renderApp("/workflows");
  await waitFor(() => {
    expect(screen.getByTestId("profile")).toHaveValue("main");
  });
  fireEvent.click(screen.getByTestId("profile"));
  const broken = await screen.findByRole("option", { name: /broken \(can't load\)/ });
  expect(broken).toHaveAttribute("data-combobox-disabled", "true");
  fireEvent.click(screen.getByRole("option", { name: "other" }));
  await waitFor(() => {
    expect(currentLocation()).toBe("/workflows?profile=other");
  });
  fireEvent.click(screen.getByRole("link", { name: "Runs" }));
  await waitFor(() => {
    expect(currentLocation()).toBe("/runs?profile=other");
  });
  await waitFor(() => {
    expect(calls.some((c) => c.path === "/api/runs" && c.search.get("profile") === "other")).toBe(
      true,
    );
  });
});

// -- UI-SHELL-3 ---------------------------------------------------------------------------------

test("UI_SHELL_3 workflows list shows validity, trust and errors with line and column", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows": () => [
      workflow(),
      workflow({ name: "bad", path: "/x/bad.yaml", valid: false, trusted: false }),
    ],
    "GET /api/workflows/bad": () =>
      workflow({
        name: "bad",
        valid: false,
        errors: [
          { path: "steps[0].uses", message: "unknown step type 'nope'", line: 3, column: 7 },
        ],
      }),
  });
  renderApp("/workflows?profile=main");
  const table = await screen.findByTestId("workflows");
  const [, good, bad] = within(table).getAllByRole("row");
  if (good === undefined || bad === undefined) throw new Error("expected two rows");
  expect(within(good).getByText("valid")).toBeInTheDocument();
  expect(within(good).getByText("trusted")).toBeInTheDocument();
  expect(within(good).getByText("inbox")).toBeInTheDocument();
  expect(within(bad).getByText("invalid")).toBeInTheDocument();
  expect(within(bad).getByText("needs review")).toBeInTheDocument();
  fireEvent.click(within(bad).getByRole("link", { name: "bad" }));
  const errors = await screen.findByTestId("errors");
  expect(errors).toHaveTextContent("3:7: steps[0].uses: unknown step type 'nope'");
});

// -- UI-SHELL-4 / -5 ----------------------------------------------------------------------------

test("UI_SHELL_4 review shows yaml, hash and safety report, then trusts that hash", async () => {
  let trusted = false;
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/two-step": () => workflow({ trusted }),
    "GET /api/workflows": () => [workflow({ trusted })],
    "POST /api/workflows/two-step/validate": () => ({
      valid: true,
      errors: [],
      sha256: SHA,
      trusted: false,
      safety: [{ step_id: "list", kind: "command", detail: "ls -la $path" }],
    }),
    "POST /api/workflows/two-step/trust": () => {
      trusted = true;
      return { name: "two-step", sha256: SHA, trusted: true };
    },
  });
  renderApp("/workflows/two-step?profile=main");
  const review = await screen.findByTestId("review");
  expect(within(review).getByText(/name: two-step/)).toBeInTheDocument();
  expect(within(review).getByText(SHA)).toBeInTheDocument();
  expect(await within(review).findByText("ls -la $path")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled();
  expect(screen.getByText("Review it first.")).toBeInTheDocument();
  fireEvent.click(within(review).getByRole("button", { name: "Trust this workflow" }));
  await waitFor(() => {
    expect(screen.queryByTestId("review")).not.toBeInTheDocument();
  });
  const trust = calls.find((c) => c.path.endsWith("/trust"));
  expect(trust?.body).toEqual({ sha256: SHA });
  expect(trust?.search.get("profile")).toBe("main");
});

test("UI_SHELL_4 empty safety report is still shown", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/two-step": () => workflow({ trusted: false }),
    "POST /api/workflows/two-step/validate": () => ({
      valid: true,
      errors: [],
      sha256: SHA,
      trusted: false,
      safety: [],
    }),
  });
  renderApp("/workflows/two-step?profile=main");
  expect(await screen.findByText(/No findings/)).toBeInTheDocument();
});

test("UI_SHELL_4 a changed file reloads the review instead of trusting", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/two-step": () => workflow({ trusted: false }),
    "POST /api/workflows/two-step/validate": () => ({
      valid: true,
      errors: [],
      sha256: SHA,
      trusted: false,
      safety: [],
    }),
    "POST /api/workflows/two-step/trust": () => err(409, "hash_mismatch"),
  });
  renderApp("/workflows/two-step?profile=main");
  await screen.findByText(/No findings/);
  fireEvent.click(screen.getByRole("button", { name: "Trust this workflow" }));
  expect(await screen.findByText(/changed since it was shown/)).toBeInTheDocument();
  expect(screen.getByTestId("review")).toBeInTheDocument();
});

test("UI_SHELL_5 start is disabled for invalid workflows and never asks about trust", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/two-step": () => workflow({ valid: false }),
  });
  renderApp("/workflows/two-step?profile=main");
  expect(await screen.findByText("Fix the errors first.")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Start run" })).toBeDisabled();
  expect(screen.queryByText(/Trust/)).not.toBeInTheDocument();
});

// -- UI-SHELL-6 ---------------------------------------------------------------------------------

test("UI_SHELL_6 vars form requires missing vars and starts with vars and hash", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/workflows/two-step": () => workflow(),
    "POST /api/runs": () => ({ status: 201, body: { id: "R9" } }),
    "GET /api/runs/R9": () => runDetail({ id: "R9", state: "queued" }),
  });
  renderApp("/workflows/two-step?profile=main");
  const start = await screen.findByRole("button", { name: "Start run" });
  expect(start).toBeDisabled();
  expect(screen.getByText("Set inbox.")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "mode" })).toHaveAttribute("placeholder", "copy");
  fireEvent.change(screen.getByRole("textbox", { name: /inbox/ }), {
    target: { value: "/srv/inbox" },
  });
  expect(start).toBeEnabled();
  fireEvent.click(start);
  await waitFor(() => {
    expect(currentLocation()).toBe("/runs/R9?profile=main");
  });
  expect(calls.find((c) => c.method === "POST" && c.path === "/api/runs")?.body).toEqual({
    profile: "main",
    workflow: "two-step",
    vars: { inbox: "/srv/inbox" },
    sha256: SHA,
  });
});

// -- UI-SHELL-7 / -8 ----------------------------------------------------------------------------

test("UI_SHELL_7 run page streams steps and log, dedupes by seq, closes on run.finished", async () => {
  mockApi({ "GET /api/profiles": () => PROFILES, "GET /api/runs/R1": () => runDetail() });
  renderApp("/runs/R1?profile=main");
  await screen.findByTestId("steps");
  const source = FakeEventSource.last("/api/runs/R1/events");
  act(() => {
    source.emit("run.started", ev(1, "run.started", null, {}), 1);
    source.emit("run.step.started", ev(2, "run.step.started", "import", {}), 2);
    source.emit(
      "run.step.log",
      ev(3, "run.step.log", "import", { stream: "stdout", line: "imported: A - B" }),
      3,
    );
    source.emit(
      "run.step.log",
      ev(3, "run.step.log", "import", { stream: "stdout", line: "imported: A - B" }),
      3,
    );
    source.emit(
      "run.step.log",
      ev(4, "run.step.log", "import", { stream: "stderr", line: "warning!" }),
      4,
    );
  });
  expect(within(screen.getByTestId("step-import")).getByText("running")).toBeInTheDocument();
  const lines = screen.getAllByTestId("log-line");
  expect(lines).toHaveLength(2); // seq 3 applied once
  expect(lines[0]).toHaveAttribute("data-stream", "stdout");
  expect(lines[1]).toHaveAttribute("data-stream", "stderr");
  expect(screen.getByRole("button", { name: "Cancel" })).toBeInTheDocument();
  act(() => {
    source.emit(
      "run.step.finished",
      ev(5, "run.step.finished", "import", { status: "succeeded" }),
      5,
    );
    source.emit(
      "run.finished",
      ev(6, "run.finished", null, { status: "failed", error: "boom" }),
      6,
    );
  });
  expect(source.readyState).toBe(2);
  expect(within(screen.getByTestId("step-import")).getByText("succeeded")).toBeInTheDocument();
  expect(within(screen.getByTestId("step-list")).getByText("skipped")).toBeInTheDocument();
  expect(screen.getByText("boom")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
});

test("UI_SHELL_8 cancel posts to the run's cancel route", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/runs/R1": () => runDetail({ state: "queued" }),
    "POST /api/runs/R1/cancel": () => ({
      status: 202,
      body: { id: "R1", status: "cancel_requested" },
    }),
  });
  renderApp("/runs/R1?profile=main");
  fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
  expect(await screen.findByRole("button", { name: "Cancelling…" })).toBeDisabled();
  expect(calls.some((c) => c.method === "POST" && c.path === "/api/runs/R1/cancel")).toBe(true);
});

// -- UI-SHELL-9 ---------------------------------------------------------------------------------

test("UI_SHELL_9 runs list refetches on dashboard events", async () => {
  let runs = [runDetail({ state: "queued" })];
  mockApi({ "GET /api/profiles": () => PROFILES, "GET /api/runs": () => runs });
  renderApp("/?profile=main");
  const table = await screen.findByTestId("runs");
  expect(within(table).getByText("queued")).toBeInTheDocument();
  expect(within(table).getByRole("link", { name: "two-step" })).toHaveAttribute(
    "href",
    "/runs/R1?profile=main",
  );
  runs = [runDetail({ state: "succeeded", finished_at: "2026-11-11T12:01:31Z" })];
  act(() => {
    FakeEventSource.last("/api/events").emit("run.finished", ev(9, "run.finished", null, {}));
  });
  expect(await within(table).findByText("succeeded")).toBeInTheDocument();
  expect(within(table).getByText("1 min 30 s")).toBeInTheDocument();
});

// -- UI-SHELL-10 --------------------------------------------------------------------------------

test("UI_SHELL_10 log view keeps only visible rows in the DOM for 10,000 lines", () => {
  const lines: LogLine[] = Array.from({ length: 10_000 }, (_, i) => ({
    seq: i + 1,
    ts: "2026-11-11T12:00:00Z",
    step: "s",
    stream: "stdout",
    text: `line ${String(i)}`,
  }));
  render(
    <Providers client={newQueryClient()} env="test">
      <LogView lines={lines} />
    </Providers>,
  );
  const rendered = screen.queryAllByTestId("log-line").length;
  expect(rendered).toBeGreaterThan(0);
  expect(rendered).toBeLessThan(100);
});

// -- UI-SHELL-11 / -12 --------------------------------------------------------------------------

test("UI_SHELL_11 beets concepts link to the beets docs", () => {
  render(
    <Providers client={newQueryClient()} env="test">
      <MemoryRouter>
        <BeetsTerm concept="query" />
      </MemoryRouter>
    </Providers>,
  );
  expect(screen.getByRole("link", { name: "query" })).toHaveAttribute("href", BEETS_DOCS.query);
});

test("UI_SHELL_12 color scheme follows the OS", () => {
  media.prefersDark = true;
  render(
    <Providers client={newQueryClient()} env="test">
      <p>x</p>
    </Providers>,
  );
  expect(document.documentElement).toHaveAttribute("data-mantine-color-scheme", "dark");
});
