import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, err, mockApi, renderApp } from "../test-utils";
import { distancePercent, parseIds, seconds } from "./ImportDecisionView";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-13T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";

function candidate(id: string, distance: number, over: Partial<S["ImportCandidate"]> = {}) {
  return {
    id,
    source: "MusicBrainz",
    artist: "Band",
    album: `Live ${id}`,
    year: 2001,
    country: "XE",
    label: "Label",
    catalognum: "CAT-1",
    media: "CD",
    tracks: 2,
    distance,
    penalties: [],
    mapping: [
      { item: "/in/Live/01.flac", title_before: "one", title_after: "One", track: 1, length: 61 },
      { item: "/in/Live/02.flac", title_before: "Two", title_after: "Two", track: 2, length: 125 },
    ],
    missing: [],
    unmatched: [],
    url: `https://musicbrainz.org/release/${id}`,
    ...over,
  } satisfies S["ImportCandidate"];
}

function decision(over: Partial<S["ImportDecision"]> = {}): S["ImportDecision"] {
  return {
    id: "d1",
    profile: "main",
    run_id: RUN,
    step_id: "import",
    kind: "match",
    path: "/in/Live",
    fingerprint: "fp",
    items: [{ path: "/in/Live/01.flac", length: 61, tags: { title: "one", track: 1 } }],
    candidates: [candidate("c2", 0.4), candidate("c1", 0.125, { missing: ["Three"] })],
    duplicates: [],
    options: {},
    set_fields: {},
    state: "pending",
    error: null,
    created_at: T0,
    deadline: null,
    on_timeout: "skip",
    action: null,
    candidate_id: null,
    decided_by: null,
    decided_at: null,
    note: null,
    album_id: null,
    ...over,
  };
}

const importItem = (d: S["ImportDecision"] = decision()): S["QueueItem"] => ({
  kind: "import",
  id: d.id,
  profile: "main",
  run_id: d.run_id,
  workflow: "imports",
  step_id: d.step_id,
  created_at: T0,
  import_decision: d,
});

function setup(d: S["ImportDecision"], extra: Record<string, (call: Call) => unknown> = {}) {
  return mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [importItem(d)],
    "POST /api/imports/d1/decision": () => ({ job_id: "j1" }),
    "POST /api/imports/d1/lookup": () => ({ job_id: "j2" }),
    ...extra,
  });
}

const bodies = (calls: Call[], path: string) =>
  calls.filter((c) => c.method === "POST" && c.path === path).map((c) => c.body);

test("IMPORT_UI_15 helpers", () => {
  expect(distancePercent(0.125)).toBe("12.5 %");
  expect(seconds(125)).toBe("2:05");
  expect(seconds(null)).toBe("");
  expect(parseIds(" a, b\nc ")).toEqual(["a", "b", "c"]);
});

test("IMPORT_UI_15 candidates best first, track table, import this match", async () => {
  const { calls } = setup(decision());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("import-decision");
  expect(within(card).getByTestId("import-path")).toHaveTextContent("/in/Live");
  expect(within(card).getByTestId("import-items")).toHaveTextContent("one");
  const listed = within(card).getAllByTestId("candidate");
  expect(listed.map((c) => c.dataset.candidate)).toEqual(["c1", "c2"]);
  expect(listed[0]).toHaveTextContent("distance 12.5 %");
  expect(listed[0]).toHaveTextContent("MusicBrainz · 2001 · XE · Label · CAT-1 · CD · 2 tracks");
  expect(within(card).getAllByRole("link", { name: "open on MusicBrainz" })[0]).toHaveAttribute(
    "href",
    "https://musicbrainz.org/release/c1",
  );
  const tracks = within(card).getByTestId("track-table");
  expect(tracks).toHaveTextContent("One");
  expect(tracks).toHaveTextContent("1:01");
  expect(within(card).getByTestId("missing-tracks")).toHaveTextContent("Three");
  fireEvent.click(within(card).getByRole("radio", { name: /Live c2/ }));
  expect(within(card).queryByTestId("missing-tracks")).not.toBeInTheDocument();
  fireEvent.change(within(card).getByLabelText("Note (optional)"), { target: { value: "ok" } });
  fireEvent.click(within(card).getByRole("button", { name: "Import this match" }));
  expect(await within(card).findByTestId("decision-sent")).toHaveTextContent("Importing it.");
  expect(bodies(calls, "/api/imports/d1/decision")).toEqual([
    { action: "candidate", candidate_id: "c2", note: "ok" },
  ]);
});

test("IMPORT_UI_15 import as-is and skip", async () => {
  const { calls } = setup(decision());
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("import-decision");
  fireEvent.click(within(card).getByRole("button", { name: "Skip" }));
  expect(await within(card).findByTestId("decision-sent")).toHaveTextContent("Skipping it.");
  expect(bodies(calls, "/api/imports/d1/decision")).toEqual([
    { action: "skip", candidate_id: null, note: null },
  ]);
});

test("IMPORT_UI_15 search by ID and by artist and album", async () => {
  const { calls } = setup(decision({ candidates: [] }));
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("import-decision");
  expect(within(card).getByTestId("no-candidates")).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "Import this match" })).toBeDisabled();
  expect(within(card).getByRole("link", { name: "search_ids" })).toHaveAttribute(
    "href",
    expect.stringContaining("search-ids"),
  );
  fireEvent.change(within(card).getByPlaceholderText("release ID or URL"), {
    target: { value: "mb-1, mb-2" },
  });
  fireEvent.click(within(card).getByRole("button", { name: "Search by ID" }));
  expect(await within(card).findByTestId("lookup-sent")).toBeInTheDocument();
  fireEvent.change(within(card).getByLabelText("Artist"), { target: { value: " Band " } });
  fireEvent.click(within(card).getByRole("button", { name: "Search by artist and album" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/imports/d1/lookup")).toEqual([
      { ids: ["mb-1", "mb-2"] },
      { artist: "Band" },
    ]);
  });
  fireEvent.click(within(card).getByRole("button", { name: "Import as-is" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/imports/d1/decision")).toEqual([
      { action: "asis", candidate_id: null, note: null },
    ]);
  });
});

test("IMPORT_UI_15 an error shows with look up again", async () => {
  const { calls } = setup(decision({ error: "the folder changed; look it up again" }), {
    "POST /api/imports/d1/decision": () => err(409, "busy", "being worked on"),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("import-decision");
  expect(within(card).getByTestId("import-error")).toHaveTextContent("the folder changed");
  fireEvent.click(within(card).getByRole("button", { name: "Look up again" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/imports/d1/lookup")).toEqual([{}]);
  });
  fireEvent.click(within(card).getByRole("button", { name: "Skip" }));
  expect(await within(card).findByText("being worked on")).toBeInTheDocument();
});

test("IMPORT_UI_15 a duplicate shows the existing album and the four choices", async () => {
  const dup = decision({
    kind: "duplicate",
    candidates: [candidate("c1", 0.05)],
    duplicates: [
      { id: "a9", path: "/music/Band/Live", fields: { albumartist: "Band", album: "Live" } },
    ],
    deadline: "2026-11-20T12:00:00Z",
    on_timeout: "asis",
  });
  const { calls } = setup(dup);
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("queue-item");
  expect(within(card).getByText("Duplicate album")).toBeInTheDocument();
  expect(within(card).getByTestId("duplicates")).toHaveTextContent("Band - Live");
  expect(within(card).getByTestId("deadline")).toHaveTextContent("it will skip automatically");
  for (const name of ["Skip", "Keep both", "Remove old", "Merge"]) {
    expect(within(card).getByRole("button", { name })).toBeInTheDocument();
  }
  expect(within(card).queryByRole("button", { name: "Import as-is" })).not.toBeInTheDocument();
  fireEvent.click(within(card).getByRole("button", { name: "Keep both" }));
  await waitFor(() => {
    expect(bodies(calls, "/api/imports/d1/decision")).toEqual([
      { action: "keep", candidate_id: null, note: null },
    ]);
  });
});
