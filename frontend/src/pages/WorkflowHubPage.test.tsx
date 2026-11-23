import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { currentLocation, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];

function detail(name: string, over: Partial<S["WorkflowDetail"]> = {}): S["WorkflowDetail"] {
  return {
    name,
    path: `/w/${name}.yaml`,
    sha256: "abc",
    valid: true,
    trusted: true,
    vars: [],
    missing_vars: [],
    yaml: "",
    errors: [],
    called: [],
    steps: [{ id: "import", uses: "beets.import", may_wait: true }],
    ...over,
  };
}

const FOLDER: S["InboxFolder"] = {
  workflow: "copying",
  folder: "/music/inbox",
  trigger_state: "armed",
  detail: null,
  omitted: 0,
  entries: [
    { path: "Done", state: "imported", changed_at: null, run_id: "R1" },
    { path: "New", state: "waiting", changed_at: null, run_id: null },
    { path: "Broke", state: "failed", changed_at: null, run_id: "R2" },
  ],
};
const OTHER_FOLDER: S["InboxFolder"] = {
  ...FOLDER,
  workflow: "elsewhere",
  folder: "/music/other",
  entries: [{ path: "Theirs", state: "waiting", changed_at: null, run_id: null }],
};

function setup(extra: Record<string, () => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [],
    "GET /api/workflows": () => [detail("copying"), detail("plain")],
    "GET /api/workflows/copying": () => detail("copying"),
    "GET /api/workflows/plain": () => detail("plain"),
    "GET /api/profiles/main/inbox": () => [FOLDER, OTHER_FOLDER],
    "GET /api/pipeline": () => ({ lanes: [], starting: [] }),
    ...extra,
  });
}

test("UI_REFRESH_26 Inbox tab before Pipeline: pending first, imported below and dimmed", async () => {
  setup();
  renderApp("/workflows/copying?profile=main");
  const tab = await screen.findByTestId("inbox-tab");
  const labels = within(screen.getByTestId("workflow-tabs"))
    .getAllByRole("tab")
    .map((t) => t.textContent);
  expect(labels).toEqual(["Inbox", "Pipeline", "Runs", "Details"]);
  // Pipeline stays the default.
  expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(tab);
  const pending = await screen.findByTestId("inbox-pending");
  expect(within(pending).getByText("New")).toBeInTheDocument();
  expect(within(pending).getByText("Broke")).toBeInTheDocument();
  expect(within(pending).queryByText("Done")).toBeNull();
  const imported = screen.getByTestId("inbox-imported");
  expect(within(imported).getByText("Done")).toBeInTheDocument();
  expect(imported).toHaveStyle({ opacity: "0.55" });
  expect(within(imported).queryByRole("button", { name: /Import again/ })).toBeNull();
  // Only this workflow's folders.
  expect(screen.queryByText("Theirs")).toBeNull();
  expect(currentLocation()).toContain("tab=inbox");
});

test("UI_REFRESH_26 no Inbox tab without a watched folder", async () => {
  setup();
  renderApp("/workflows/plain?profile=main&tab=inbox");
  await screen.findByTestId("workflow-tabs");
  await waitFor(() => {
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
  });
  expect(screen.queryByTestId("inbox-tab")).toBeNull();
});

test("UI_REFRESH_25 Start run from the page header opens Pipeline", async () => {
  const { calls } = setup({ "POST /api/runs": () => ({ id: "R9" }) });
  renderApp("/workflows/copying?profile=main&tab=details");
  fireEvent.click(await screen.findByTestId("start-run-open"));
  const form = await screen.findByTestId("start-dialog");
  fireEvent.click(within(form).getByRole("button", { name: "Start run" }));
  await waitFor(() => {
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/runs")).toBe(true);
  });
  const post = calls.find((c) => c.method === "POST" && c.path === "/api/runs");
  expect(post?.body).toMatchObject({ profile: "main", workflow: "copying", sha256: "abc" });
  await waitFor(() => {
    expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
  });
});

test("UI_REFRESH_25 an untrusted workflow can't start from the header and says why", async () => {
  setup({ "GET /api/workflows/copying": () => detail("copying", { trusted: false }) });
  renderApp("/workflows/copying?profile=main");
  fireEvent.click(await screen.findByTestId("start-run-open"));
  const form = await screen.findByTestId("start-dialog");
  expect(within(form).getByRole("button", { name: "Start run" })).toBeDisabled();
  expect(within(form).getByText("Review it first.")).toBeInTheDocument();
});
