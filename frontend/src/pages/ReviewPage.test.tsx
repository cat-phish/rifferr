import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import {
  type Call,
  FakeEventSource,
  currentLocation,
  err,
  mockApi,
  renderApp,
} from "../test-utils";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-13T12:00:00Z";
const OLD_RUN = "01AAAAAAAAAAAAAAAAAAAAAAAA";
const NEW_RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

function changeset(id: string, run: string, over: Partial<S["ChangeSet"]> = {}): S["ChangeSet"] {
  return {
    id,
    profile: "main",
    run_id: run,
    step_id: "suffix",
    source: { kind: "step", name: "tags.script" },
    rationale: "release type suffix",
    created_at: T0,
    targets: [
      {
        target: { scope: "album", id: "a1" },
        changes: [
          { field: "album", op: "set", old: "Live", new: "Live (EP)" },
          { field: "albumdisambig", op: "set", old: null, new: "EP" },
        ],
      },
    ],
    ...over,
  };
}

function csItem(
  id: string,
  run: string,
  state: NonNullable<S["QueueItem"]["state"]> = "proposed",
  cs: S["ChangeSet"] = changeset(id, run),
): S["QueueItem"] {
  return {
    busy: false,
    kind: "changeset",
    id,
    profile: "main",
    run_id: run,
    workflow: run === NEW_RUN ? "tagging" : "older",
    step_id: "suffix",
    created_at: T0,
    changeset: cs,
    state,
    gate: null,
  };
}

function gate(over: Partial<S["ReviewGate"]> = {}): S["ReviewGate"] {
  return {
    id: "g1",
    profile: "main",
    run_id: NEW_RUN,
    step_id: "check",
    state: "pending",
    albums: [
      {
        target: { scope: "album", id: "a1" },
        label: "Band - Live",
        message: "<b>check the suffix</b>",
        shown: { albumtype: "ep", genres: ["Rock", "Live"] },
        changes: [
          {
            target: { scope: "album", id: "a1" },
            field: "album",
            before: "Live",
            after: "Live (EP)",
            existed_before: true,
          },
        ],
        conflicted: ["cs9"],
        imported_by_run: true,
      },
    ],
    on_reject: "undo",
    on_timeout: "continue",
    created_at: T0,
    deadline: "2026-11-20T12:00:00Z",
    decided_at: null,
    decided_by: null,
    note: null,
    ...over,
  };
}

const gateItem = (g: S["ReviewGate"] = gate()): S["QueueItem"] => ({
  busy: false,
  kind: "gate",
  id: g.id,
  profile: "main",
  run_id: g.run_id,
  workflow: "tagging",
  step_id: g.step_id,
  created_at: T0,
  changeset: null,
  state: null,
  gate: g,
});

function current(album: string | null = "Live"): S["CurrentValues"] {
  return {
    targets: [
      {
        target: { scope: "album", id: "a1" },
        label: "Band - Live",
        missing: false,
        fields: album === null ? {} : { album },
      },
    ],
  };
}

interface Opts {
  queue?: () => S["QueueItem"][];
  current?: (call: Call) => unknown;
  extra?: Record<string, (call: Call) => unknown>;
}

function setup(o: Opts = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": o.queue ?? (() => []),
    "GET /api/changesets/cs1/current": o.current ?? (() => current()),
    "GET /api/changesets/cs2/current": () => current("Live!"),
    "GET /api/changesets/cs3/current": () => current(),
    ...o.extra,
  });
}

function nth<T>(xs: T[], i: number): T {
  const x = xs[i];
  if (x === undefined) throw new Error(`no element ${String(i)}`);
  return x;
}
const posts = (calls: Call[], path: string) =>
  calls.filter((c) => c.method === "POST" && c.path === path);
const item = (id: string) =>
  screen.getAllByTestId("queue-item").find((e) => e.dataset.item === id) as HTMLElement;

// -- REVIEW-QUEUE-11 ----------------------------------------------------------------------

test("REVIEW_QUEUE_11 queue in one section per step, oldest first, with album, run link and nav count", async () => {
  const later = { ...csItem("cs1", NEW_RUN), created_at: "2026-11-13T12:05:00Z" };
  setup({
    queue: () => [later, csItem("cs3", NEW_RUN), gateItem()],
    extra: {
      [`GET /api/runs/${NEW_RUN}`]: () =>
        runDetail({
          steps: [step("suffix"), step("check")],
          lanes: [laneOf("A", "waiting", ["cs1"], "suffix")],
        }),
    },
  });
  renderApp("/review?profile=main");
  const nav = await within(screen.getByRole("navigation")).findByRole("link", {
    name: /^Workflows/,
  });
  await waitFor(() => {
    expect(within(nav).getByTestId("review-count")).toHaveTextContent("3");
  });
  // One tab per stage, in step order, with its count (UI-REFRESH-13); the first is open.
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toEqual([
      "suffix2",
      "check1",
    ]);
  });
  expect(screen.getAllByTestId("step-section").map((g) => g.dataset.step)).toEqual(["suffix"]);
  const first = nth(screen.getAllByTestId("step-section"), 0);
  // The newer cs1 goes below cs3.
  expect(
    within(first)
      .getAllByTestId("queue-item")
      .map((e) => e.dataset.item),
  ).toEqual(["cs3", "cs1"]);
  await waitFor(() => {
    expect(within(item("cs1")).getByTestId("item-album")).toHaveTextContent("Band - A");
  });
  expect(within(item("cs1")).getByTestId("item-run")).toHaveAttribute(
    "href",
    `/runs/${NEW_RUN}?profile=main`,
  );
});

test("REVIEW_QUEUE_19 an item a decision job holds is neither listed nor counted", async () => {
  setup({
    queue: () => [csItem("cs1", NEW_RUN), { ...csItem("cs3", NEW_RUN), busy: true }],
    extra: {
      [`GET /api/runs/${NEW_RUN}`]: () =>
        runDetail({ steps: [step("suffix")], lanes: [laneOf("A", "waiting", ["cs1"], "suffix")] }),
    },
  });
  renderApp("/review?profile=main");
  const nav = await within(screen.getByRole("navigation")).findByRole("link", {
    name: /^Workflows/,
  });
  await waitFor(() => {
    expect(within(nav).getByTestId("review-count")).toHaveTextContent("1");
  });
  await waitFor(() => {
    expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["cs1"]);
  });
});

test("REVIEW_QUEUE_11 the queue updates from /api/events", async () => {
  let queue = [csItem("cs1", NEW_RUN)];
  setup({ queue: () => queue });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  queue = [];
  await act(async () => {
    FakeEventSource.last("/api/events").emit("changeset.applied", {
      seq: 5,
      run_id: NEW_RUN,
      type: "changeset.applied",
      data: { changeset_id: "cs1" },
    });
    await Promise.resolve();
  });
  expect(await screen.findByTestId("queue-empty")).toBeInTheDocument();
});

test("UI_REFRESH_13 switching tabs; an emptied tab stays until another is picked", async () => {
  let queue = [csItem("cs1", NEW_RUN), gateItem()];
  setup({ queue: () => queue });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: /check/ }));
  expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["g1"]);
  queue = [csItem("cs1", NEW_RUN)];
  await act(async () => {
    FakeEventSource.last("/api/events").emit("review.resolved", {
      seq: 6,
      run_id: NEW_RUN,
      type: "review.resolved",
      data: { gate_id: "g1" },
    });
    await Promise.resolve();
  });
  // The open stage has nothing left; it stays until another tab is picked.
  expect(await screen.findByTestId("stage-empty")).toHaveTextContent(
    "Nothing waiting at this step.",
  );
  expect(screen.getByRole("tab", { name: /check/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: /suffix/ }));
  await screen.findByTestId("queue-item");
  expect(screen.queryByRole("tab", { name: /check/ })).toBeNull();
});

test("UI_REFRESH_13 ?stage= opens that stage's tab", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN), gateItem()] });
  renderApp("/review?profile=main&workflow=tagging&stage=check");
  await screen.findByTestId("queue-item");
  expect(screen.getByRole("tab", { name: /check/ })).toHaveAttribute("aria-selected", "true");
  expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["g1"]);
  // An old /review link goes on to the item's workflow page (UI-REFRESH-23).
  expect(currentLocation()).toBe("/workflows/tagging?profile=main&stage=check");
});

function summary(name: string): S["WorkflowSummary"] {
  return {
    name,
    path: `/w/${name}.yaml`,
    sha256: "x",
    valid: true,
    trusted: true,
    vars: [],
    missing_vars: [],
  };
}

function detail(name: string, steps: [string, boolean][]): S["WorkflowDetail"] {
  return {
    ...summary(name),
    yaml: "",
    errors: [],
    called: [],
    steps: steps.map(([id, may_wait]) => ({ id, uses: "x.y", may_wait })),
  };
}

const WORKFLOWS = {
  "GET /api/workflows": () => [summary("older"), summary("tagging")],
  "GET /api/workflows/tagging": () =>
    detail("tagging", [
      ["import", true],
      ["suffix", true],
      ["write", false],
      ["check", true],
    ]),
  "GET /api/workflows/older": () => detail("older", [["suffix", true]]),
};

test("UI_REFRESH_13 UI_REFRESH_24 a tab per step, always there; new items never switch tabs", async () => {
  let queue = [csItem("cs1", NEW_RUN)];
  setup({ queue: () => queue, extra: WORKFLOWS });
  renderApp("/workflows/tagging?profile=main");
  await screen.findByTestId("queue-item");
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toEqual([
      "import0",
      "suffix1",
      "write0",
      "check0",
    ]);
  });
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
  queue = [csItem("cs1", NEW_RUN), gateItem()];
  await act(async () => {
    FakeEventSource.last("/api/events").emit("review.requested", {
      seq: 7,
      run_id: NEW_RUN,
      type: "review.requested",
      data: { gate_id: "g1" },
    });
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toEqual([
      "import0",
      "suffix1",
      "write0",
      "check1",
    ]);
  });
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: /import/ }));
  expect(await screen.findByTestId("stage-empty")).toHaveTextContent(
    "Nothing waiting at this step.",
  );
});

test("UI_REFRESH_13 items of a step that isn't in the workflow get an Other tab", async () => {
  setup({
    queue: () => [{ ...csItem("cs1", NEW_RUN), step_id: null }],
    extra: WORKFLOWS,
  });
  renderApp("/workflows/tagging?profile=main");
  await screen.findByTestId("queue-item");
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toEqual([
      "import0",
      "suffix0",
      "write0",
      "check0",
      "Other1",
    ]);
  });
  expect(screen.getByRole("tab", { name: /Other/ })).toHaveAttribute("aria-selected", "true");
});

// -- UI-REFRESH-24 ------------------------------------------------------------------------

function pipelineLane(
  key: string,
  position: string | null,
  holds: S["LaneHold"][] = [],
): S["PipelineLane"] {
  return {
    run_id: NEW_RUN,
    key,
    label: `Artist - ${key}`,
    state: holds.length > 0 ? "waiting" : "active",
    position,
    holds,
    steps: {},
    updated_at: new Date().toISOString(),
  };
}

test("UI_REFRESH_24 albums at a step that doesn't wait; review items first and in the bar", async () => {
  let lanes = [
    pipelineLane("A", "write"),
    pipelineLane("B", "suffix", [{ kind: "changeset", ref: "cs1" }]),
  ];
  setup({
    queue: () => [csItem("cs1", NEW_RUN)],
    extra: {
      ...WORKFLOWS,
      "GET /api/pipeline": () => ({
        lanes,
        starting: [
          {
            run_id: "R2",
            state: "queued",
            input: "/in/New Album",
            created_at: new Date().toISOString(),
          },
        ],
      }),
    },
  });
  renderApp("/workflows/tagging?profile=main");
  await screen.findByTestId("queue-item");
  // The waiting item's tab opens first; its held lane isn't listed again under it.
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
  expect(screen.queryByTestId("at-this-step")).toBeNull();
  expect(screen.getAllByTestId("waiting-chip").map((c) => c.textContent)).toEqual(["suffix · 1"]);
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-albums").map((c) => c.textContent)).toEqual(["+1", "+1"]);
  });
  fireEvent.click(screen.getByRole("tab", { name: /write/ }));
  const rows = await screen.findAllByTestId("pipeline-album");
  expect(rows.map((r) => r.textContent)).toEqual([expect.stringContaining("Artist - A")]);
  fireEvent.click(screen.getByRole("tab", { name: /import/ }));
  expect((await screen.findByTestId("pipeline-album")).textContent).toContain(
    "Run starting: New Album",
  );
  // A lane past the last step gets a trailing Finishing tab.
  lanes = [...lanes, pipelineLane("C", null)];
  await act(async () => {
    FakeEventSource.last("/api/events").emit("lane.finished", {
      seq: 8,
      run_id: NEW_RUN,
      type: "lane.finished",
      data: {},
    });
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toContain("Finishing0+1");
  });
  // The bar opens a waiting stage from any tab.
  fireEvent.click(screen.getByTestId("waiting-chip"));
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
});

// -- UI-REFRESH-22 ------------------------------------------------------------------------

test("UI_REFRESH_23 /workflows lists each workflow with its stages; nav and cards open one", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN), csItem("cs3", NEW_RUN)], extra: WORKFLOWS });
  renderApp("/workflows?profile=main");
  // Two workflows: the overview, one card per workflow with its stage counts.
  await waitFor(() => {
    expect(screen.getAllByTestId("review-workflow-card")).toHaveLength(2);
  });
  const tagging = nth(screen.getAllByTestId("review-workflow-card"), 1);
  expect(within(tagging).getByTestId("workflow-waiting")).toHaveTextContent("2 items waiting");
  expect(
    within(tagging)
      .getAllByTestId("overview-stage")
      .map((t) => t.textContent),
  ).toEqual(["import 0", "suffix 2", "check 0"]);
  expect(screen.queryByTestId("queue-item")).toBeNull();
  expect(screen.getAllByTestId("review-workflow-link").map((l) => l.textContent)).toEqual([
    "older0",
    "tagging2",
  ]);
  fireEvent.click(within(tagging).getByRole("link", { name: /suffix/ }));
  await screen.findAllByTestId("queue-item");
  expect(screen.getByTestId("workflow-title")).toHaveTextContent("tagging");
  expect(screen.getByRole("tab", { name: /suffix/ })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("link", { name: /older/ }));
  await waitFor(() => {
    expect(screen.getByTestId("workflow-title")).toHaveTextContent("older");
  });
  expect(screen.getAllByTestId("stage-tab").map((t) => t.textContent)).toEqual(["suffix0"]);
  expect(screen.queryByTestId("queue-item")).toBeNull();
});

test("UI_REFRESH_23 hub links to editing; a workflow page has Pipeline, Runs and Details tabs", async () => {
  const { calls } = setup({
    queue: () => [csItem("cs1", NEW_RUN)],
    extra: { ...WORKFLOWS, "GET /api/runs": () => [] },
  });
  renderApp("/workflows?profile=main");
  await waitFor(() => {
    expect(screen.getByRole("link", { name: "Edit workflows" })).toHaveAttribute(
      "href",
      "/workflow-files?profile=main",
    );
  });
  await waitFor(() => {
    expect(screen.getAllByTestId("review-workflow-card")).toHaveLength(2);
  });
  fireEvent.click(
    within(nth(screen.getAllByTestId("review-workflow-card"), 1)).getByRole("link", {
      name: "tagging",
    }),
  );
  await screen.findAllByTestId("queue-item");
  expect(screen.getByRole("tab", { name: "Pipeline" })).toHaveAttribute("aria-selected", "true");
  fireEvent.click(screen.getByRole("tab", { name: "Runs" }));
  await waitFor(() => {
    const runs = calls.filter((c: Call) => c.method === "GET" && c.path === "/api/runs");
    expect(runs.some((c) => c.search.get("workflow") === "tagging")).toBe(true);
  });
  expect(currentLocation()).toContain("tab=runs");
  expect(screen.queryByTestId("queue-item")).toBeNull();
});

test("UI_REFRESH_22 the workflow list under Workflows folds away", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN)], extra: WORKFLOWS });
  renderApp("/workflows/tagging?profile=main");
  await screen.findAllByTestId("review-workflow-link");
  const toggle = screen.getByTestId("review-toggle");
  expect(toggle).toHaveAccessibleName("Hide workflows");
  fireEvent.click(toggle);
  expect(toggle).toHaveAccessibleName("Show workflows");
  await waitFor(() => {
    for (const l of screen.queryAllByTestId("review-workflow-link")) expect(l).not.toBeVisible();
  });
  // Folding doesn't leave the page.
  expect(screen.getByTestId("workflow-title")).toHaveTextContent("tagging");
  fireEvent.click(toggle);
  await waitFor(() => {
    expect(nth(screen.getAllByTestId("review-workflow-link"), 0)).toBeVisible();
  });
});

// -- REVIEW-QUEUE-12 ----------------------------------------------------------------------

test("REVIEW_QUEUE_12 field changes with current values; accept only the checked ones", async () => {
  const { calls } = setup({
    queue: () => [csItem("cs1", NEW_RUN)],
    current: () => current("Live!"),
    extra: {
      "POST /api/changesets/cs1/decision": () => ({ status: 202, body: { job_id: "j1" } }),
    },
  });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  const rows = await screen.findAllByTestId("field-change");
  expect(rows.map((r) => r.dataset.field)).toEqual(["album", "albumdisambig"]);
  await waitFor(() => {
    expect(within(nth(rows, 0)).getByTestId("now")).toHaveTextContent("Live! (changed)");
  });
  expect(within(nth(rows, 0)).getByTestId("now")).toHaveAttribute("data-changed", "true");
  expect(within(nth(rows, 0)).getByText("Band - Live")).toBeInTheDocument();
  expect(within(nth(rows, 0)).getByTestId("old")).toHaveTextContent("Live");
  expect(within(nth(rows, 0)).getByTestId("proposed")).toHaveTextContent("Live (EP)");
  // albumdisambig is absent now, as the step saw it: not marked.
  expect(within(nth(rows, 1)).getByTestId("now")).toHaveTextContent("—");
  expect(within(nth(rows, 1)).getByTestId("now")).not.toHaveAttribute("data-changed");

  fireEvent.click(screen.getByRole("checkbox", { name: "Accept albumdisambig of Band - Live" }));
  // The proposed value is a diff of the value the step saw (UI-REFRESH-4).
  const added = within(nth(rows, 0)).getByTestId("proposed").querySelector('[data-seg="added"]');
  expect(added).toHaveTextContent("(EP)");
  // The note is behind "Add note" (UI-REFRESH-11).
  expect(screen.queryByLabelText("Note (optional)")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Add note" }));
  fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: "no disambig" } });
  fireEvent.click(screen.getByRole("button", { name: "Accept selected" }));
  await waitFor(() => {
    expect(posts(calls, "/api/changesets/cs1/decision")).toHaveLength(1);
  });
  expect(nth(posts(calls, "/api/changesets/cs1/decision"), 0).body).toEqual({
    decision: "accept",
    accepted: [0],
    note: "no disambig",
  });
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(screen.queryAllByTestId("queue-item")).toHaveLength(0);
  });
});

test("REVIEW_QUEUE_12 accept all, reject, and errors", async () => {
  const { calls } = setup({
    queue: () => [csItem("cs1", NEW_RUN), csItem("cs3", NEW_RUN)],
    extra: {
      "POST /api/changesets/cs1/decision": () => ({ status: 202, body: { job_id: "j1" } }),
      "POST /api/changesets/cs3/decision": () => err(409, "already_resolved", "cs3 is applied"),
    },
  });
  renderApp("/review?profile=main");
  await screen.findAllByTestId("queue-item");
  fireEvent.click(within(item("cs1")).getByRole("button", { name: "Accept" }));
  await waitFor(() => {
    expect(posts(calls, "/api/changesets/cs1/decision")[0]?.body).toEqual({
      decision: "accept",
      note: null,
    });
  });
  fireEvent.click(within(item("cs3")).getByRole("button", { name: "Reject" }));
  expect(await within(item("cs3")).findByText("cs3 is applied")).toBeInTheDocument();
  expect(nth(posts(calls, "/api/changesets/cs3/decision"), 0).body).toEqual({
    decision: "reject",
    note: null,
  });
  // Nothing checked: nothing to accept.
  fireEvent.click(within(item("cs3")).getByRole("checkbox", { name: "Select all" }));
  expect(within(item("cs3")).getByRole("button", { name: "Accept selected" })).toBeDisabled();
});

test("REVIEW_QUEUE_12 conflicted ChangeSet: expected, current, proposed; propose again", async () => {
  const { calls } = setup({
    queue: () => [csItem("cs2", NEW_RUN, "conflicted")],
    extra: {
      "POST /api/changesets/cs2/repropose": () => ({ status: 202, body: { job_id: "j2" } }),
    },
  });
  renderApp("/review?profile=main");
  await screen.findByTestId("conflict-note");
  const table = screen.getByTestId("field-changes");
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent),
  ).toEqual(["Target", "Field", "Expected", "Current", "Proposed"]);
  expect(within(table).queryAllByRole("checkbox")).toHaveLength(0);
  const album = nth(within(table).getAllByTestId("field-change"), 0);
  await waitFor(() => {
    expect(within(album).getByTestId("now")).toHaveTextContent("Live! (changed)");
  });
  expect(screen.queryByRole("button", { name: /Accept/ })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Propose again on current values" }));
  await waitFor(() => {
    expect(posts(calls, "/api/changesets/cs2/repropose")).toHaveLength(1);
  });
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(screen.queryAllByTestId("queue-item")).toHaveLength(0);
  });
});

test("REVIEW_QUEUE_12 a target no longer in the library", async () => {
  setup({
    queue: () => [csItem("cs1", NEW_RUN)],
    current: () => ({
      targets: [{ target: { scope: "album", id: "a1" }, label: "album a1", missing: true }],
    }),
  });
  renderApp("/review?profile=main");
  const rows = await screen.findAllByTestId("field-change");
  await waitFor(() => {
    expect(within(nth(rows, 0)).getByTestId("now")).toHaveTextContent("no longer in the library");
  });
});

// -- REVIEW-QUEUE-13 ----------------------------------------------------------------------

test("REVIEW_QUEUE_13 gate card: albums, changes, deadline, continue with a note", async () => {
  const { calls } = setup({
    queue: () => [gateItem()],
    extra: {
      "POST /api/reviews/g1/decision": (call) => ({
        ...gate(),
        state:
          (call.body as { decision: string }).decision === "continue" ? "continued" : "rejected",
      }),
    },
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  const album = within(card).getByTestId("gate-album");
  expect(within(album).getByText("Band - Live")).toBeInTheDocument();
  expect(within(album).getByText("imported by this run")).toBeInTheDocument();
  expect(within(album).getByText("Rock; Live")).toBeInTheDocument();
  const changes = within(album).getByTestId("gate-changes");
  expect(within(changes).getByTestId("gate-change")).toHaveTextContent("Live (EP)");
  expect(within(album).getByRole("link", { name: "cs9" })).toHaveAttribute(
    "href",
    "/review?profile=main&item=cs9",
  );
  expect(within(card).getByTestId("deadline")).toHaveTextContent("it will continue automatically");
  expect(within(card).getByTestId("reject-says")).toHaveTextContent(
    "Reject undoes this run's tag changes, then stops the run.",
  );
  fireEvent.click(within(card).getByRole("button", { name: "Add note" }));
  fireEvent.change(within(card).getByLabelText("Note (optional)"), {
    target: { value: "looks right" },
  });
  fireEvent.click(within(card).getByRole("button", { name: "Continue" }));
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(card).not.toBeInTheDocument();
  });
  expect(nth(posts(calls, "/api/reviews/g1/decision"), 0).body).toEqual({
    decision: "continue",
    note: "looks right",
  });
});

test("REVIEW_QUEUE_13 reject with on_reject stop, and a gate that can't be decided", async () => {
  setup({
    queue: () => [gateItem(gate({ on_reject: "stop", on_timeout: null }))],
    extra: { "POST /api/reviews/g1/decision": () => err(409, "not_waiting", "run is running") },
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByTestId("reject-says")).toHaveTextContent(
    "Reject stops the run. Changes it already applied stay.",
  );
  expect(within(card).getByTestId("deadline")).toHaveTextContent("it will reject automatically");
  fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
  expect(await within(card).findByText("run is running")).toBeInTheDocument();
});

// -- REVIEW-QUEUE-14 ----------------------------------------------------------------------

test("REVIEW_QUEUE_14 accept all per section asks for confirmation with the count", async () => {
  const { calls } = setup({
    queue: () => [
      csItem("cs1", NEW_RUN),
      csItem("cs3", OLD_RUN),
      csItem("cs2", NEW_RUN, "conflicted"),
    ],
    extra: {
      "POST /api/changesets/cs1/decision": () => ({ job_id: "j1" }),
      "POST /api/changesets/cs3/decision": () => ({ job_id: "j2" }),
    },
  });
  renderApp("/workflows/tagging?profile=main");
  await screen.findAllByTestId("queue-item");
  // One workflow at a time (UI-REFRESH-22).
  const sec = await screen.findByTestId("step-section");
  expect(within(sec).getByTestId("step-label")).toHaveTextContent("suffix");
  expect(screen.getByTestId("workflow-title")).toHaveTextContent("tagging");
  fireEvent.click(within(sec).getByRole("button", { name: "Reject all" }));
  expect(await screen.findByTestId("bulk-confirm")).toHaveTextContent(
    "Reject 1 ChangeSet of suffix? Conflicted ChangeSets are left alone.",
  );
  expect(posts(calls, "/api/changesets/cs1/decision")).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Reject 1" }));
  await waitFor(() => {
    expect(posts(calls, "/api/changesets/cs1/decision")[0]?.body).toMatchObject({
      decision: "reject",
    });
  });
  expect(posts(calls, "/api/changesets/cs3/decision")).toHaveLength(0);
});

// -- REVIEW-QUEUE-16 ----------------------------------------------------------------------

test("REVIEW_QUEUE_16 ?item= opens that item; ?run= shows one run", async () => {
  setup({ queue: () => [csItem("cs3", OLD_RUN), csItem("cs1", NEW_RUN), gateItem()] });
  renderApp("/review?profile=main&item=g1");
  await screen.findAllByTestId("queue-item");
  // The item's stage tab opens (UI-REFRESH-13).
  expect(screen.getByRole("tab", { name: /check/ })).toHaveAttribute("aria-selected", "true");
  expect(item("g1")).toHaveAttribute("data-focused", "true");
  expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["g1"]);
  expect(currentLocation()).toMatch(/^\/workflows\/tagging\?profile=main&item=g1/);
});

test("REVIEW_QUEUE_16 ?run= filters, and a gone item says so", async () => {
  setup({ queue: () => [csItem("cs3", OLD_RUN), csItem("cs1", NEW_RUN)] });
  renderApp(`/review?profile=main&run=${OLD_RUN}&item=gone`);
  await screen.findByTestId("queue-item");
  expect(screen.getAllByTestId("queue-item").map((g) => g.dataset.item)).toEqual(["cs3"]);
  expect(screen.getByTestId("focus-gone")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Show every run" })).toBeInTheDocument();
});

// -- REVIEW-QUEUE-17 ----------------------------------------------------------------------

test("REVIEW_QUEUE_17 values and messages are text, never HTML", async () => {
  const html = "<img src=x onerror=alert(1)>";
  const cs = changeset("cs1", NEW_RUN, {
    rationale: "<script>bad()</script>",
    targets: [
      {
        target: { scope: "album", id: "a1" },
        changes: [{ field: "comments", op: "set", old: null, new: html }],
      },
    ],
  });
  setup({ queue: () => [csItem("cs1", NEW_RUN, "proposed", cs), gateItem()] });
  const { container } = renderApp("/review?profile=main");
  await screen.findAllByTestId("queue-item");
  expect(screen.getByText(html)).toBeInTheDocument();
  expect(screen.getByText("<script>bad()</script>")).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: /check/ }));
  expect(await screen.findByText("<b>check the suffix</b>")).toBeInTheDocument();
  expect(container.querySelector("img")).toBeNull();
  expect(container.querySelector("script")).toBeNull();
  expect(container.querySelector("b")).toBeNull();
});

// -- REVIEW-QUEUE-15 ----------------------------------------------------------------------

function runDetail(over: Partial<S["RunDetail"]> = {}): S["RunDetail"] {
  return {
    id: NEW_RUN,
    profile: "main",
    workflow: "tagging",
    workflow_hash: null,
    trigger: "manual",
    state: "waiting_review",
    trusted: true,
    error: null,
    created_at: T0,
    started_at: T0,
    finished_at: null,
    steps: [{ id: "suffix", uses: "tags.script", status: "succeeded", outputs: {}, passes: 1 }],
    undoable: 0,
    ...over,
  };
}

function step(id: string): S["StepState"] {
  return { id, uses: "tags.script", status: "succeeded", outputs: {}, passes: 1 };
}

const runEvent = (seq: number, type: string, data: Record<string, unknown> = {}) => ({
  seq,
  ts: T0,
  run_id: NEW_RUN,
  type,
  step_id: null,
  data,
});

test("REVIEW_QUEUE_15 a waiting run links to its items in the queue", async () => {
  setup({ extra: { [`GET /api/runs/${NEW_RUN}`]: () => runDetail() } });
  renderApp(`/runs/${NEW_RUN}?profile=main`);
  const waiting = await screen.findByTestId("waiting-review");
  expect(within(waiting).getByRole("link")).toHaveAttribute(
    "href",
    `/review?profile=main&run=${NEW_RUN}`,
  );
  await act(async () => {
    FakeEventSource.last(`/api/runs/${NEW_RUN}/events`).emit(
      "run.resumed",
      runEvent(9, "run.resumed"),
    );
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(screen.queryByTestId("waiting-review")).not.toBeInTheDocument();
  });
});

async function finishedRunWithUndo(job: (call: Call) => unknown) {
  let run = runDetail({ state: "succeeded", finished_at: T0, undoable: 2 });
  const mocked = setup({
    extra: {
      [`GET /api/runs/${NEW_RUN}`]: () => run,
      [`POST /api/runs/${NEW_RUN}/undo`]: job,
    },
  });
  renderApp(`/runs/${NEW_RUN}?profile=main`);
  fireEvent.click(await screen.findByRole("button", { name: "Undo this run" }));
  expect(await screen.findByTestId("undo-confirm")).toHaveTextContent(
    "Undo the 2 tag changes this run applied, newest first?",
  );
  run = { ...run, undoable: 0 };
  return mocked;
}

async function jobFinished(data: Record<string, unknown>) {
  await act(async () => {
    const stream = [...FakeEventSource.instances]
      .reverse()
      .find((s) => s.url === "/api/events" && s.readyState !== 2);
    stream?.emit("job.finished", runEvent(20, "job.finished", data));
    await Promise.resolve();
  });
}

test("REVIEW_QUEUE_15 undo this run: confirm, then what was undone", async () => {
  const { calls } = await finishedRunWithUndo(() => ({ status: 202, body: { job_id: "u1" } }));
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => {
    expect(posts(calls, `/api/runs/${NEW_RUN}/undo`)).toHaveLength(1);
  });
  expect(await screen.findByTestId("undo-pending")).toBeInTheDocument();
  await jobFinished({ job_id: "other", state: "succeeded", result: { undone: [] } });
  expect(screen.getByTestId("undo-pending")).toBeInTheDocument();
  await jobFinished({
    job_id: "u1",
    state: "succeeded",
    result: { undone: ["e2", "e1"], stopped_at: null, conflicts: [], problems: [] },
  });
  expect(await screen.findByTestId("undo-report")).toHaveTextContent("Undone: 2 changes reverted.");
});

test("REVIEW_QUEUE_15 undo stops at a conflict and says which", async () => {
  await finishedRunWithUndo(() => ({ status: 202, body: { job_id: "u1" } }));
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await screen.findByTestId("undo-pending");
  await jobFinished({
    job_id: "u1",
    state: "failed",
    error: "undo stopped at journal entry e1",
    result: {
      undone: ["e2"],
      stopped_at: "e1",
      conflicts: [{ target: "album a1", field: "year", expected: 2002, current: 1999 }],
      problems: [],
    },
  });
  const report = await screen.findByTestId("undo-report");
  expect(report).toHaveTextContent("Reverted 1 change, then stopped at journal entry e1");
  expect(report).toHaveTextContent("album a1 year: expected 2002, now 1999");
});

test("LIBRARY_MOVE_15 undo refused for changed files; undo anyway sends them as force", async () => {
  let n = 0;
  const { calls } = await finishedRunWithUndo(() => {
    n += 1;
    return { status: 202, body: { job_id: `u${String(n)}` } };
  });
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await screen.findByTestId("undo-pending");
  await jobFinished({
    job_id: "u1",
    state: "failed",
    error: "undo stopped at journal entry e1",
    result: {
      undone: [],
      stopped_at: "e1",
      conflicts: [],
      problems: [],
      changed_files: ["/music/A/01. One.flac", "/music/A/02. Two.flac"],
      skipped_files: [],
    },
  });
  const changed = await screen.findByTestId("undo-changed-files");
  expect(changed).toHaveTextContent("changed since they were moved");
  const button = within(changed).getByRole("button", { name: "Undo anyway" });
  expect(button).toBeDisabled();
  fireEvent.click(within(changed).getByLabelText("Undo /music/A/01. One.flac anyway"));
  fireEvent.click(button);
  await waitFor(() => {
    expect(posts(calls, `/api/runs/${NEW_RUN}/undo`).map((c) => c.body)).toEqual([
      { force: [] },
      { force: ["/music/A/01. One.flac"] },
    ]);
  });
  await jobFinished({
    job_id: "u2",
    state: "succeeded",
    result: { undone: ["e1"], stopped_at: null, conflicts: [], problems: [] },
  });
  expect(await screen.findByTestId("undo-report")).toHaveTextContent("Undone: 1 change reverted.");
});

test("REVIEW_QUEUE_15 no undo for a run without applied changes", async () => {
  setup({
    extra: {
      [`GET /api/runs/${NEW_RUN}`]: () => runDetail({ state: "succeeded", finished_at: T0 }),
    },
  });
  renderApp(`/runs/${NEW_RUN}?profile=main`);
  await screen.findByTestId("steps");
  expect(screen.queryByRole("button", { name: "Undo this run" })).not.toBeInTheDocument();
  expect(screen.queryByTestId("waiting-review")).not.toBeInTheDocument();
});

// -- F-UI-REFRESH step sections ---------------------------------------------------------------

function laneOf(
  key: string,
  state: S["LaneOut"]["state"],
  refs: string[],
  position: string | null,
) {
  return {
    key,
    label: `Band - ${key}`,
    state,
    position,
    holds: refs.map((ref) => ({ kind: "changeset" as const, ref })),
    steps: {},
  } satisfies S["LaneOut"];
}

test("UI_REFRESH_10_main_actions_sit_in_the_header_row", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN)] });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  const actions = within(item("cs1")).getByTestId("item-actions");
  expect(within(actions).getByRole("button", { name: "Accept" })).toBeInTheDocument();
  expect(within(actions).getByRole("button", { name: "Reject" })).toBeInTheDocument();
});

test("UI_REFRESH_11_note_hidden_until_used_and_kept", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN)] });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  expect(screen.queryByLabelText("Note (optional)")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Add note" }));
  fireEvent.change(screen.getByLabelText("Note (optional)"), { target: { value: "kept" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /Accept albumdisambig/ }));
  expect(screen.getByLabelText("Note (optional)")).toHaveValue("kept");
});

test("UI_REFRESH_12_long_tables_fold_after_8_rows", async () => {
  const changes = Array.from({ length: 11 }, (_, n) => ({
    field: `f${String(n)}`,
    op: "set" as const,
    old: "a",
    new: "b",
  }));
  const cs = changeset("cs1", NEW_RUN, {
    targets: [{ target: { scope: "album", id: "a1" }, changes }],
  });
  setup({ queue: () => [csItem("cs1", NEW_RUN, "proposed", cs)] });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  expect(screen.getAllByTestId("field-change")).toHaveLength(8);
  fireEvent.click(screen.getByRole("button", { name: "Show all 11" }));
  expect(screen.getAllByTestId("field-change")).toHaveLength(11);
});

test("UI_REFRESH_16_item_links_and_accept_all_work_inside_step_sections", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN), csItem("cs3", NEW_RUN)] });
  renderApp("/review?profile=main&item=cs3");
  const sec = await screen.findByTestId("step-section");
  expect(item("cs3")).toHaveAttribute("data-focused", "true");
  expect(within(sec).getByRole("button", { name: "Accept all" })).toBeInTheDocument();
});

test("UI_REFRESH_14_UI_REFRESH_20_a_decided_item_leaves_no_working_row", async () => {
  setup({
    queue: () => [csItem("cs1", NEW_RUN)],
    extra: { "POST /api/changesets/cs1/decision": () => ({ job_id: "job-1" }) },
  });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  fireEvent.click(within(item("cs1")).getByRole("button", { name: "Accept" }));
  await waitFor(() => {
    expect(screen.queryByTestId("step-section")).not.toBeInTheDocument();
  });
  expect(screen.queryByText("Working")).not.toBeInTheDocument();
});

test("UI_REFRESH_20_a_decided_item_leaves_at_once_and_comes_back_if_its_job_ends_without_it", async () => {
  setup({
    queue: () => [csItem("cs1", NEW_RUN), csItem("cs3", NEW_RUN)],
    extra: { "POST /api/changesets/cs1/decision": () => ({ job_id: "job-1" }) },
  });
  renderApp("/review?profile=main");
  await screen.findAllByTestId("queue-item");
  fireEvent.click(within(item("cs1")).getByRole("button", { name: "Accept" }));
  // Gone at once, while the job is still to run; the next item is right there.
  await waitFor(() => {
    expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["cs3"]);
  });
  // The job ends but the item still waits (it failed): it comes back.
  await act(async () => {
    FakeEventSource.last("/api/events").emit("job.finished", {
      seq: 9,
      run_id: NEW_RUN,
      type: "job.finished",
      data: { job_id: "job-1", state: "failed" },
    });
    await Promise.resolve();
  });
  await waitFor(() => {
    expect(
      screen
        .getAllByTestId("queue-item")
        .map((e) => e.dataset.item)
        .sort(),
    ).toEqual(["cs1", "cs3"]);
  });
});

test("UI_REFRESH_15_withdrawn_no_run_lane_counts_on_the_review_page", async () => {
  setup({ queue: () => [csItem("cs1", NEW_RUN)] });
  renderApp("/review?profile=main");
  await screen.findByTestId("queue-item");
  expect(screen.queryByTestId("lane-counts")).not.toBeInTheDocument();
  expect(screen.queryByTestId("run-group")).not.toBeInTheDocument();
});

test("UI_REFRESH_20_a_succeeded_decision_job_does_not_bring_the_item_back", async () => {
  setup({
    queue: () => [csItem("cs1", NEW_RUN), csItem("cs3", NEW_RUN)],
    extra: { "POST /api/changesets/cs1/decision": () => ({ job_id: "job-1" }) },
  });
  renderApp("/review?profile=main");
  await screen.findAllByTestId("queue-item");
  fireEvent.click(within(item("cs1")).getByRole("button", { name: "Accept" }));
  await waitFor(() => {
    expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["cs3"]);
  });
  // The decision job is done, but the run applies the move afterwards: cs1 still waits
  // in the queue for a moment and must stay hidden, whatever else arrives meanwhile.
  await act(async () => {
    const es = FakeEventSource.last("/api/events");
    es.emit("job.finished", {
      seq: 9,
      run_id: NEW_RUN,
      type: "job.finished",
      data: { job_id: "job-1", state: "succeeded" },
    });
    es.emit("changeset.proposed", {
      seq: 10,
      run_id: NEW_RUN,
      type: "changeset.proposed",
      data: {},
    });
    await Promise.resolve();
  });
  await new Promise((r) => setTimeout(r, 50));
  expect(screen.getAllByTestId("queue-item").map((e) => e.dataset.item)).toEqual(["cs3"]);
});

// -- UI-REFRESH-27 ------------------------------------------------------------------------

test("UI_REFRESH_27 a failed album stays at the step it failed at, with its error", async () => {
  setup({
    queue: () => [],
    extra: {
      ...WORKFLOWS,
      "GET /api/pipeline": () => ({
        lanes: [],
        starting: [],
        failed: [
          {
            run_id: "R9",
            key: "album-1",
            label: "Artist - Failed",
            step: "write",
            error: "{{ vars.x }}: 'x' is not set",
            failed_at: new Date().toISOString(),
          },
        ],
      }),
    },
  });
  renderApp("/workflows/tagging?profile=main");
  // With nothing waiting and no live albums, the tab with the failure opens.
  await waitFor(() => {
    expect(screen.getByRole("tab", { name: /write/ })).toHaveAttribute("aria-selected", "true");
  });
  expect(screen.getByTestId("stage-failed").textContent).toBe("1 failed");
  const row = await screen.findByTestId("failed-album");
  expect(row.textContent).toContain("Artist - Failed");
  expect(screen.getByTestId("failed-error").textContent).toBe("{{ vars.x }}: 'x' is not set");
  expect(row.querySelector("a")?.getAttribute("href")).toContain("/runs/R9");
});
