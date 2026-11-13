import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, expect, test } from "vitest";
import type { components } from "../api/schema";
import { DEFAULT_COLUMNS } from "../lib/grid";
import { type Call, FakeEventSource, mockApi, renderApp } from "../test-utils";

type S = components["schemas"];
const STREAM = "/api/profiles/main/events";

function profiles(grid: S["GridColumns"] = { album: null, item: null }): S["ProfileSummary"][] {
  return [
    { name: "main", backend: "beets", vars: [], secrets: [], error: null, grid_columns: grid },
  ];
}

function page(year = 2001, over: Partial<S["AlbumSnapshot"]["fields"]> = {}): S["LibraryPage"] {
  return {
    albums: [
      {
        id: "a1",
        path: "/m/Band/Live",
        fields: { albumartist: "Band", album: "Live", year, genres: ["Rock"], ...over },
        items: [
          { id: "i1", path: "/m/1.flac", fields: { track: 1, title: "One", artist: "Band" } },
          { id: "i2", path: "/m/2.flac", fields: { track: 2, title: "Two", artist: "Band" } },
        ],
      },
      {
        id: "a2",
        path: "/m/Band/Other",
        fields: { albumartist: "Band", album: "Other", year: 1999 },
        items: [{ id: "i3", path: "/m/3.flac", fields: { track: 1, title: "Three" } }],
      },
    ],
    album_fields: ["album", "albumartist", "genres", "year"],
    item_fields: ["artist", "title", "track"],
    truncated: false,
  };
}

interface Opts {
  library?: (call: Call) => unknown;
  check?: (call: Call) => unknown;
  changes?: (call: Call) => unknown;
  recent?: () => unknown;
  grid?: S["GridColumns"];
}

function setup(o: Opts = {}) {
  return mockApi({
    "GET /api/profiles": () => profiles(o.grid),
    "GET /api/profiles/main/library": o.library ?? (() => page()),
    "POST /api/profiles/main/changes/check":
      o.check ??
      ((call) => ({ problems: [], targets: (call.body as { targets: unknown[] }).targets })),
    "POST /api/profiles/main/changes":
      o.changes ??
      (() => ({ status: 202, body: { changesets: ["cs1"], job_id: "j1", queued_behind: [] } })),
    "GET /api/profiles/main/changes": o.recent ?? (() => []),
    "POST /api/profiles/main/journal/e1/undo": () => ({ status: 202, body: { job_id: "j2" } }),
  });
}

beforeEach(() => {
  window.localStorage.clear();
});

const rowOf = (key: string) =>
  screen.getAllByTestId("grid-row").find((r) => r.dataset.row === key) as HTMLElement;
const cellOf = (key: string, field: string) =>
  within(rowOf(key))
    .getAllByTestId("cell")
    .find((c) => c.dataset.field === field) as HTMLElement;
const calls = (all: Call[], method: string, path: string) =>
  all.filter((c) => c.method === method && c.path === path);

async function editCell(key: string, field: string, value: string) {
  fireEvent.doubleClick(cellOf(key, field));
  const input = await screen.findByLabelText(`${field} value`);
  fireEvent.change(input, { target: { value } });
  fireEvent.keyDown(input, { key: "Enter" });
}

async function emit(type: string, data: Record<string, unknown>, seq: number) {
  await act(async () => {
    FakeEventSource.last(STREAM).emit(type, { seq, type, run_id: "profile:main", data }, seq);
    await Promise.resolve();
  });
}

test("TAG_GRID_1 library page: query, scope, grid and nav", async () => {
  const { calls: all } = setup();
  renderApp("/library?profile=main");
  expect(await screen.findByRole("link", { name: "Library" })).toBeInTheDocument();
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  expect(screen.getByRole("link", { name: "beets query" })).toHaveAttribute(
    "href",
    "https://beets.readthedocs.io/en/stable/reference/query.html",
  );
  expect(cellOf("album:a1", "album")).toHaveTextContent("Live");
  fireEvent.change(screen.getByLabelText("Query"), { target: { value: "year:2001" } });
  await waitFor(() => {
    expect(calls(all, "GET", "/api/profiles/main/library").at(-1)?.search.get("query")).toBe(
      "year:2001",
    );
  });
  fireEvent.click(screen.getByText("Items"));
  await waitFor(() => {
    expect(calls(all, "GET", "/api/profiles/main/library").at(-1)?.search.get("scope")).toBe(
      "item",
    );
  });
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(3);
  });
  expect(cellOf("item:i1", "title")).toHaveTextContent("One");
});

test("TAG_GRID_3 default columns: built-in, profile, remembered choice", async () => {
  setup({ grid: { album: ["album", "year"], item: null } });
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  const headers = () =>
    within(screen.getByTestId("tag-grid"))
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
  expect(headers()).toEqual(["album", "year"]);
  const stored = JSON.parse(window.localStorage.getItem("rifferr.grid.main.item") ?? "{}") as {
    columns: string[];
  };
  expect(stored.columns).toEqual(DEFAULT_COLUMNS.item);
  // A choice made in the UI wins over the profile default next time.
  cleanup();
  window.localStorage.setItem(
    "rifferr.grid.main.album",
    JSON.stringify({ columns: ["albumartist"], widths: { albumartist: 222 } }),
  );
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  expect(headers()).toEqual(["albumartist"]);
  expect(cellOf("album:a1", "albumartist")).toHaveStyle({ width: "222px" });
});

test("TAG_GRID_4 albums expand to their editable items", async () => {
  setup();
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  fireEvent.click(within(rowOf("album:a1")).getByRole("button", { name: "Expand" }));
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(4);
  });
  expect(cellOf("item:i2", "title")).toHaveTextContent("Two");
  await editCell("item:i2", "title", "Deux");
  expect(cellOf("item:i2", "title")).toHaveAttribute("data-pending", "true");
});

test("TAG_GRID_5 edits stay pending, shown as before → after", async () => {
  const { calls: all } = setup();
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  await editCell("album:a1", "year", "2002");
  const cell = cellOf("album:a1", "year");
  expect(cell).toHaveAttribute("data-pending", "true");
  expect(cell).toHaveTextContent("2001 → 2002");
  expect(calls(all, "POST", "/api/profiles/main/changes")).toHaveLength(0);
  expect(screen.getByTestId("pending")).toHaveTextContent("1 pending edit");
  // Back to the value it had: no edit.
  await editCell("album:a1", "year", "2001");
  expect(cellOf("album:a1", "year")).not.toHaveAttribute("data-pending");
});

test("TAG_GRID_5 multi-value fields are edited as a list of entries", async () => {
  const { calls: all } = setup();
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  fireEvent.click(screen.getByLabelText("Album columns", { selector: "input" }));
  fireEvent.click(await screen.findByRole("option", { name: "genres" }));
  fireEvent.doubleClick(cellOf("album:a1", "genres"));
  const input = await screen.findByLabelText("genres entries", { selector: "input" });
  fireEvent.change(input, { target: { value: "Pop; Jazz" } });
  fireEvent.keyDown(input, { key: "Enter" });
  fireEvent.click(screen.getByRole("button", { name: "Done" }));
  await waitFor(() => {
    expect(calls(all, "POST", "/api/profiles/main/changes/check").length).toBeGreaterThan(0);
  });
  const body = calls(all, "POST", "/api/profiles/main/changes/check").at(-1)?.body as {
    targets: { changes: { new: unknown }[] }[];
  };
  expect(body.targets[0]?.changes[0]?.new).toEqual(["Rock", "Pop; Jazz"]);
});

test("TAG_GRID_6 Set field adds a pending edit to each selected row", async () => {
  setup();
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  fireEvent.click(screen.getByLabelText("Select all"));
  fireEvent.click(screen.getByRole("button", { name: "Set field…" }));
  fireEvent.change(await screen.findByLabelText("Field", { selector: "input" }), {
    target: { value: "year" },
  });
  fireEvent.change(screen.getByLabelText("Value"), { target: { value: "2010" } });
  fireEvent.click(screen.getByRole("button", { name: "Add pending edits" }));
  await waitFor(() => {
    expect(cellOf("album:a2", "year")).toHaveTextContent("1999 → 2010");
  });
  expect(cellOf("album:a1", "year")).toHaveTextContent("2001 → 2010");
  fireEvent.click(screen.getByRole("button", { name: "Delete field…" }));
  fireEvent.change(await screen.findByLabelText("Field", { selector: "input" }), {
    target: { value: "year" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Add pending edits" }));
  await waitFor(() => {
    expect(cellOf("album:a1", "year")).toHaveTextContent("2001 → (deleted)");
  });
});

test("TAG_GRID_7 problems show on the cell and block Apply", async () => {
  setup({
    check: (call) => ({
      problems: [
        { target: { scope: "album", id: "a1" }, field: "year", message: "expects a whole number" },
      ],
      targets: (call.body as { targets: unknown[] }).targets,
    }),
  });
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  await editCell("album:a1", "year", "soon");
  await waitFor(() => {
    expect(cellOf("album:a1", "year")).toHaveAttribute("data-problem", "true");
  });
  expect(cellOf("album:a1", "year")).toHaveAttribute("title", "expects a whole number");
  expect(screen.getByTestId("problems")).toHaveTextContent("1 problem");
  expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
});

async function applied(o: Opts = {}) {
  const api = setup(o);
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  await editCell("album:a1", "year", "2002");
  const apply = screen.getByRole("button", { name: "Apply" });
  await waitFor(() => {
    expect(apply).toBeEnabled();
  });
  fireEvent.click(apply);
  await waitFor(() => {
    expect(calls(api.calls, "POST", "/api/profiles/main/changes")).toHaveLength(1);
  });
  await waitFor(() => {
    expect(rowOf("album:a1")).toHaveAttribute("data-busy", "true");
  });
  return api;
}

test("TAG_GRID_8 Apply sends all pending edits with the values the user saw", async () => {
  const api = await applied();
  const body = calls(api.calls, "POST", "/api/profiles/main/changes")[0]?.body;
  expect(body).toEqual({
    targets: [
      {
        target: { scope: "album", id: "a1" },
        album: "a1",
        changes: [{ field: "year", op: "set", old: 2001, new: "2002" }],
      },
    ],
  });
  expect(screen.queryByTestId("pending")).not.toBeInTheDocument();
});

test("TAG_GRID_10 applied edits refresh from a new query", async () => {
  let year = 2001;
  const api = await applied({ library: () => page(year) });
  const before = calls(api.calls, "GET", "/api/profiles/main/library").length;
  year = 2002;
  await emit("changeset.applied", { changeset_id: "cs1", write_failures: [] }, 1);
  await emit("job.finished", { job_id: "j1", state: "succeeded" }, 2);
  await waitFor(() => {
    expect(calls(api.calls, "GET", "/api/profiles/main/library").length).toBeGreaterThan(before);
  });
  await waitFor(() => {
    expect(cellOf("album:a1", "year")).toHaveTextContent("2002");
  });
  expect(rowOf("album:a1")).not.toHaveAttribute("data-busy");
  expect(screen.getByTestId("notice")).toHaveTextContent("Edits applied.");
});

test("TAG_GRID_10 events before the Apply response are not lost", async () => {
  setup();
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  // The stream reports the outcome first; the page matches it once Apply returns.
  await emit("changeset.applied", { changeset_id: "cs1", write_failures: [] }, 1);
  await emit("job.finished", { job_id: "j1", state: "succeeded" }, 2);
  await editCell("album:a1", "year", "2002");
  const apply = screen.getByRole("button", { name: "Apply" });
  await waitFor(() => {
    expect(apply).toBeEnabled();
  });
  fireEvent.click(apply);
  await waitFor(() => {
    expect(screen.getByTestId("notice")).toHaveTextContent("Edits applied.");
  });
  expect(rowOf("album:a1")).not.toHaveAttribute("data-busy");
});

test("TAG_GRID_10 conflicts offer apply on top and discard", async () => {
  const api = await applied({ library: () => page(1999) });
  await emit(
    "changeset.conflicted",
    {
      changeset_id: "cs1",
      conflicts: [{ target: "album a1", field: "year", expected: 2001, current: 1999 }],
    },
    1,
  );
  const conflict = await screen.findByTestId("conflict");
  expect(conflict).toHaveTextContent("now 1999");
  await waitFor(() => {
    expect(cellOf("album:a1", "year")).toHaveTextContent("now 1999, you wanted 2002");
  });
  fireEvent.click(within(conflict).getByRole("button", { name: "Apply on top" }));
  await waitFor(() => {
    expect(cellOf("album:a1", "year")).toHaveTextContent("1999 → 2002");
  });
  expect(screen.queryByTestId("conflict")).not.toBeInTheDocument();
  await waitFor(() => {
    expect(calls(api.calls, "POST", "/api/profiles/main/changes/check").at(-1)?.body).toEqual({
      targets: [
        {
          target: { scope: "album", id: "a1" },
          album: "a1",
          changes: [{ field: "year", op: "set", old: 1999, new: "2002" }],
        },
      ],
    });
  });
});

test("TAG_GRID_10 tag-write failures show on their rows", async () => {
  await applied();
  await emit(
    "changeset.applied",
    {
      changeset_id: "cs1",
      write_failures: [{ target: "album a1", path: "/m/1.flac", error: "read-only file" }],
    },
    1,
  );
  await waitFor(() => {
    expect(rowOf("album:a1")).toHaveAttribute("title", "tags not written: read-only file");
  });
});

test("TAG_GRID_10 a failed edit job puts the edits back", async () => {
  await applied();
  await emit("job.finished", { job_id: "j1", state: "failed", error: "worker_crashed" }, 1);
  await waitFor(() => {
    expect(screen.getByTestId("notice")).toHaveTextContent("Applying failed: worker_crashed");
  });
  expect(cellOf("album:a1", "year")).toHaveAttribute("data-pending", "true");
});

function recent(state: S["ManualEdit"]["state"] = "applied"): S["ManualEdit"][] {
  return [
    {
      changeset: {
        id: "cs1",
        profile: "main",
        source: { kind: "manual", name: "ui" },
        created_at: "2026-11-12T10:00:00Z",
        targets: [
          {
            target: { scope: "album", id: "a1" },
            changes: [{ field: "year", op: "set", old: 2001, new: 2002 }],
          },
        ],
      },
      state,
      journal_id: state === "applied" ? "e1" : null,
      undo_journal_id: null,
    },
  ];
}

test("TAG_GRID_11 recent edits with Undo and its result", async () => {
  const { calls: all } = setup({ recent: () => recent() });
  renderApp("/library?profile=main");
  const item = await screen.findByTestId("recent-edit");
  expect(item).toHaveTextContent("year");
  expect(item).toHaveTextContent("applied");
  fireEvent.click(within(item).getByRole("button", { name: "Undo" }));
  await waitFor(() => {
    expect(calls(all, "POST", "/api/profiles/main/journal/e1/undo")).toHaveLength(1);
  });
  await emit(
    "job.finished",
    {
      job_id: "j2",
      state: "failed",
      result: {
        stopped_at: "e1",
        conflicts: [
          { target: "album a1", field: "year", expected: 2002, current: 2003, would_restore: 2001 },
        ],
      },
    },
    1,
  );
  await waitFor(() => {
    expect(screen.getByTestId("undo-result")).toHaveTextContent(
      "Not undone: year of album a1 is now 2003; undo would restore 2001",
    );
  });
});

test("TAG_GRID_12 preview a script on the grid's query", async () => {
  setup();
  renderApp("/library?profile=main");
  fireEvent.change(await screen.findByLabelText("Query"), { target: { value: "albumtype:ep" } });
  expect(screen.getByTestId("script-link")).toHaveAttribute(
    "href",
    "/scripts?profile=main&query=albumtype%3Aep",
  );
});

test("TAG_GRID_13 edits queued behind a running job say so", async () => {
  await applied({
    changes: () => ({
      status: 202,
      body: { changesets: ["cs1"], job_id: "j1", queued_behind: ["01RUN0000000000000000ABCDEF"] },
    }),
  });
  const queued = await screen.findByTestId("queued");
  expect(queued).toHaveTextContent("Your edits wait for run ABCDEF to finish");
  await emit("changeset.applied", { changeset_id: "cs1", write_failures: [] }, 1);
  await waitFor(() => {
    expect(screen.queryByTestId("queued")).not.toBeInTheDocument();
  });
});

test("TAG_GRID_14 values are shown as text, never as HTML", async () => {
  setup({ library: () => page(2001, { album: "<b>bold</b><img src=x onerror=alert(1)>" }) });
  renderApp("/library?profile=main");
  await waitFor(() => {
    expect(screen.getAllByTestId("grid-row")).toHaveLength(2);
  });
  const cell = cellOf("album:a1", "album");
  expect(cell).toHaveTextContent("<b>bold</b><img src=x onerror=alert(1)>");
  expect(cell.querySelector("b, img")).toBeNull();
});
