import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, FakeEventSource, err, mockApi, renderApp } from "../test-utils";

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

test("REVIEW_QUEUE_11 queue grouped by run, newest run first, with a nav count", async () => {
  setup({
    queue: () => [csItem("cs3", OLD_RUN), csItem("cs1", NEW_RUN), gateItem()],
  });
  renderApp("/review?profile=main");
  const nav = await screen.findByRole("link", { name: /Review/ });
  await waitFor(() => {
    expect(within(nav).getByTestId("review-count")).toHaveTextContent("3");
  });
  const groups = await screen.findAllByTestId("run-group");
  expect(groups.map((g) => g.dataset.run)).toEqual([NEW_RUN, OLD_RUN]);
  expect(within(nth(groups, 0)).getByRole("link", { name: "tagging" })).toHaveAttribute(
    "href",
    `/runs/${NEW_RUN}?profile=main`,
  );
  expect(
    within(nth(groups, 0))
      .getAllByTestId("queue-item")
      .map((e) => e.dataset.kind),
  ).toEqual(["changeset", "gate"]);
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
  expect(await screen.findByTestId("decision-sent")).toHaveTextContent("Accepting…");
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
  expect(await screen.findByTestId("decision-sent")).toHaveTextContent(
    "Proposing again on the current values…",
  );
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
  expect(within(changes).getByText("Live (EP)")).toBeInTheDocument();
  expect(within(album).getByRole("link", { name: "cs9" })).toHaveAttribute(
    "href",
    "/review?profile=main&item=cs9",
  );
  expect(within(card).getByTestId("deadline")).toHaveTextContent("it will continue automatically");
  expect(within(card).getByTestId("reject-says")).toHaveTextContent(
    "Reject undoes this run's tag changes, then stops the run.",
  );
  fireEvent.change(within(card).getByLabelText("Note (optional)"), {
    target: { value: "looks right" },
  });
  fireEvent.click(within(card).getByRole("button", { name: "Continue" }));
  expect(await within(card).findByTestId("decision-sent")).toHaveTextContent("Continued.");
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

test("REVIEW_QUEUE_14 accept all asks for confirmation with the count", async () => {
  const { calls } = setup({
    queue: () => [
      csItem("cs1", NEW_RUN),
      csItem("cs3", NEW_RUN),
      csItem("cs2", NEW_RUN, "conflicted"),
    ],
    extra: {
      [`POST /api/runs/${NEW_RUN}/decisions`]: () => ({
        status: 202,
        body: { job_ids: ["j1", "j2"] },
      }),
    },
  });
  renderApp("/review?profile=main");
  await screen.findAllByTestId("queue-item");
  fireEvent.click(screen.getByRole("button", { name: "Reject all" }));
  expect(await screen.findByTestId("bulk-confirm")).toHaveTextContent(
    "Reject 2 ChangeSets of this run? Conflicted ChangeSets are left alone.",
  );
  expect(posts(calls, `/api/runs/${NEW_RUN}/decisions`)).toHaveLength(0);
  fireEvent.click(screen.getByRole("button", { name: "Reject 2" }));
  await waitFor(() => {
    expect(posts(calls, `/api/runs/${NEW_RUN}/decisions`)[0]?.body).toEqual({
      decision: "reject",
    });
  });
});

// -- REVIEW-QUEUE-16 ----------------------------------------------------------------------

test("REVIEW_QUEUE_16 ?item= opens that item; ?run= shows one run", async () => {
  setup({ queue: () => [csItem("cs3", OLD_RUN), csItem("cs1", NEW_RUN), gateItem()] });
  renderApp("/review?profile=main&item=g1");
  await screen.findAllByTestId("queue-item");
  expect(item("g1")).toHaveAttribute("data-focused", "true");
  expect(item("cs1")).not.toHaveAttribute("data-focused");
});

test("REVIEW_QUEUE_16 ?run= filters, and a gone item says so", async () => {
  setup({ queue: () => [csItem("cs3", OLD_RUN), csItem("cs1", NEW_RUN)] });
  renderApp(`/review?profile=main&run=${OLD_RUN}&item=gone`);
  await screen.findByTestId("queue-item");
  expect(screen.getAllByTestId("run-group").map((g) => g.dataset.run)).toEqual([OLD_RUN]);
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
  expect(screen.getByText("<b>check the suffix</b>")).toBeInTheDocument();
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
    state: "waiting_review",
    trusted: true,
    error: null,
    created_at: T0,
    started_at: T0,
    finished_at: null,
    steps: [{ id: "suffix", uses: "tags.script", status: "succeeded", outputs: {} }],
    undoable: 0,
    ...over,
  };
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
