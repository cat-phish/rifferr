import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { type Call, mockApi, renderApp } from "../test-utils";
import { commonDir, folders, relative } from "./FileChangeSetView";

type S = components["schemas"];
const PROFILES = [{ name: "main", backend: "beets", vars: [], secrets: [], error: null }];
const T0 = "2026-11-18T12:00:00Z";
const RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB";
const OLD = "/inbox/Abbey Road";
const NEW = "/music/Beatles, The/(1969) Abbey Road [FLAC]";
const SHA = "0".repeat(64);

function effect(
  kind: S["FileEffect"]["kind"],
  src: string,
  dst: string,
  id = "item:1",
): S["FileEffect"] {
  return {
    target: { scope: kind === "item" ? "item" : "album", id },
    kind,
    src,
    dst,
    mode: "move",
    size: 10,
    sha256: SHA,
    state: "planned",
  };
}

const FILES = [
  effect("item", `${OLD}/01 Come Together.flac`, `${NEW}/01. Come Together.flac`),
  effect("item", `${OLD}/CD2/01 Here Comes.flac`, `${NEW}/Disc 2/01. Here Comes.flac`, "item:2"),
  effect("art", `${OLD}/cover.jpg`, `${NEW}/cover.jpg`, "album:1"),
  effect("artifact", `${OLD}/Scans/back.jpg`, `${NEW}/Art/back.jpg`, "album:1"),
];

function changeset(): S["ChangeSet"] {
  return {
    id: "cs1",
    profile: "main",
    run_id: RUN,
    step_id: "into-library",
    source: { kind: "step", name: "library.move" },
    rationale: `library.move: ${OLD} -> ${NEW}`,
    created_at: T0,
    targets: [
      {
        target: { scope: "album", id: "album:1" },
        changes: [{ field: "date_added", op: "set", old: null, new: "2026-11-18" }],
      },
    ],
    files: FILES,
    stamp: { field: "date_added", format: "%Y-%m-%d" },
  };
}

test("LIBRARY_MOVE_16 helpers", () => {
  expect(commonDir(["/a/b/c.flac", "/a/b/d/e.flac"])).toBe("/a/b");
  expect(relative("/a/b/d/e.flac", "/a/b")).toBe("d/e.flac");
  expect(relative("/x/y", "/a")).toBe("/x/y");
  expect(folders(FILES)).toEqual({ from: OLD, to: NEW });
});

test("LIBRARY_MOVE_16 old and new folder and names, accepted per album", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        busy: false,
        kind: "changeset",
        id: "cs1",
        profile: "main",
        run_id: RUN,
        workflow: "into-library",
        step_id: "into-library",
        created_at: T0,
        changeset: changeset(),
        state: "proposed",
      } satisfies S["QueueItem"],
    ],
    "POST /api/changesets/cs1/decision": () => ({ job_id: "j1" }),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("file-changeset");
  expect(within(card).getByTestId("old-folder")).toHaveTextContent(OLD);
  expect(within(card).getByTestId("new-folder")).toHaveTextContent(NEW);
  // One line per album; the files and the date note fold away (UI-REFRESH-21).
  expect(within(card).queryByTestId("file-effect")).toBeNull();
  expect(within(card).queryByTestId("file-stamp")).toBeNull();
  fireEvent.click(within(card).getByRole("button", { name: "Show the 4 files" }));
  expect(within(card).getByTestId("file-stamp")).toHaveTextContent("Sets date_added");
  const rows = within(card).getAllByTestId("file-effect");
  expect(rows.map((r) => r.dataset.kind)).toEqual(["item", "item", "art", "artifact"]);
  expect(within(rows[1] as HTMLElement).getByTestId("old-name")).toHaveTextContent(
    "CD2/01 Here Comes.flac",
  );
  expect(within(rows[1] as HTMLElement).getByTestId("new-name")).toHaveTextContent(
    "Disc 2/01. Here Comes.flac",
  );
  expect(within(rows[3] as HTMLElement).getByTestId("new-name")).toHaveTextContent("Art/back.jpg");
  expect(within(card).queryAllByRole("checkbox")).toEqual([]); // never file by file
  // The main actions sit in the item's header row (UI-REFRESH-10).
  const item = card.closest<HTMLElement>('[data-testid="queue-item"]') as HTMLElement;
  const actions = within(item).getByTestId("item-actions");
  fireEvent.click(within(actions).getByRole("button", { name: "Move album" }));
  // Decided: the item leaves the waiting list at once (UI-REFRESH-20).
  await waitFor(() => {
    expect(actions).not.toBeInTheDocument();
  });
  await waitFor(() => {
    const posted = calls.filter((c: Call) => c.path === "/api/changesets/cs1/decision");
    expect(posted.map((c) => c.body)).toEqual([{ decision: "accept", note: null }]);
  });
});

test("UI_REFRESH_21 a stage of library moves offers Move all N", async () => {
  const move = (id: string): S["QueueItem"] => ({
    busy: false,
    kind: "changeset",
    id,
    profile: "main",
    run_id: RUN,
    workflow: "into-library",
    step_id: "into-library",
    created_at: T0,
    changeset: { ...changeset(), id },
    state: "proposed",
  });
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [move("cs1"), move("cs2")],
    "POST /api/changesets/cs1/decision": () => ({ job_id: "j1" }),
    "POST /api/changesets/cs2/decision": () => ({ job_id: "j2" }),
  });
  renderApp("/review?profile=main");
  fireEvent.click(await screen.findByRole("button", { name: "Move all 2" }));
  expect(await screen.findByTestId("bulk-confirm")).toHaveTextContent(
    "Move 2 albums of into-library?",
  );
  fireEvent.click(screen.getByRole("button", { name: "Move 2" }));
  await waitFor(() => {
    expect(calls.filter((c) => c.method === "POST").map((c) => c.path)).toEqual([
      "/api/changesets/cs1/decision",
      "/api/changesets/cs2/decision",
    ]);
  });
});

test("LIBRARY_MOVE_18 TAG_GRID_16 the album check: cover with its size, show fields, changes marked", async () => {
  const cs: S["ChangeSet"] = {
    ...changeset(),
    album: { scope: "album", id: "album:1" },
    show: { album: ["album", "label"], item: ["track", "title", "length"] },
  };
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        busy: false,
        kind: "changeset",
        id: "cs1",
        profile: "main",
        run_id: RUN,
        workflow: "into-library",
        step_id: "into-library",
        created_at: T0,
        changeset: cs,
        state: "proposed",
      } satisfies S["QueueItem"],
    ],
    "GET /api/changesets/cs1/before": () => ({
      album_id: "album:1",
      album: { genre: "Rock" },
      items: { "item:2": { title: "here comes" } },
      folder: [{ path: "/inbox/1.flac", tags: { track: 1, title: "Come Together" } }],
    }),
    "GET /api/profiles/main/library": () => ({
      albums: [
        {
          id: "album:1",
          path: NEW,
          fields: { album: "Abbey Road", label: "Apple", genre: "Pop", albumartist: "The Beatles" },
          items: [
            {
              id: "item:1",
              path: "x",
              fields: { track: 1, title: "Come Together", length: 259.6 },
            },
            { id: "item:2", path: "y", fields: { track: 2, title: "Here Comes" } },
          ],
        },
      ],
      album_fields: [],
      item_fields: [],
      truncated: false,
      field_kinds: { length: "duration" },
    }),
  });
  renderApp("/review?profile=main");
  const check = await screen.findByTestId("album-check");
  await waitFor(() => {
    expect(within(check).getByTestId("album-check-album")).toHaveTextContent("Apple");
  });
  const albumRows = within(check).getByTestId("album-check-album").querySelectorAll("tr");
  // The show list, then the field the run changed.
  expect([...albumRows].map((r) => r.dataset.field)).toEqual(["album", "label", "genre"]);
  const genre = albumRows[2] as HTMLElement;
  expect(within(genre).getByText("changed")).toBeInTheDocument();
  expect(within(albumRows[0] as HTMLElement).queryByText("changed")).toBeNull();
  const tracks = within(check).getByTestId("album-check-items");
  const second = tracks.querySelector('tr[data-item="item:2"] td[data-field="title"]');
  expect(second?.querySelector('[data-changed="true"]')).not.toBeNull();
  const first = tracks.querySelector('tr[data-item="item:1"] td[data-field="title"]');
  expect(first?.querySelector('[data-changed="true"]')).toBeNull();
  // Durations as m:ss (TAG-GRID-16).
  const length = tracks.querySelector('tr[data-item="item:1"] td[data-field="length"]');
  expect(length).toHaveTextContent("4:20");
  // The cover at a readable size, its pixel size once loaded.
  const img = within(check).getByRole("img", { name: /^Cover of The Beatles - Abbey Road$/ });
  expect(img.getAttribute("src")).toBe("/api/albums/album%3A1/art?profile=main");
  Object.defineProperty(img, "naturalWidth", { value: 1200 });
  Object.defineProperty(img, "naturalHeight", { value: 1000 });
  fireEvent.load(img);
  expect(within(check).getByTestId("cover-size")).toHaveTextContent("1200×1000 px");
  expect(within(check).getByRole("button", { name: "Change cover" })).toBeInTheDocument();
  expect(calls.some((c: Call) => c.path === "/api/profiles/main/library")).toBe(true);
});

function queueItem(cs: S["ChangeSet"]): S["QueueItem"] {
  return {
    busy: false,
    kind: "changeset",
    id: cs.id,
    profile: "main",
    run_id: RUN,
    workflow: "w",
    step_id: cs.step_id ?? null,
    created_at: T0,
    changeset: cs,
    state: "proposed",
  };
}

test("COVERS_RESIZE_6 a file ChangeSet that also changes the cover shows its rationale", async () => {
  const resize: S["ChangeSet"] = {
    ...changeset(),
    step_id: "art-size",
    source: { kind: "step", name: "covers.resize" },
    rationale: "cover 3000x3000 -> 1200x1200; original kept as cover-hires.jpg",
    stamp: null,
    targets: [
      {
        target: { scope: "album", id: "album:1" },
        changes: [{ field: "art_sha256", op: "set", old: SHA, new: "1".repeat(64) }],
      },
    ],
    files: [
      {
        ...effect("artifact", "/art/00/orig.jpg", `${NEW}/cover-hires.jpg`, "album:1"),
        mode: "copy",
      },
    ],
  };
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [queueItem(resize)],
  });
  renderApp("/review?profile=main");
  expect(await screen.findByTestId("file-changeset-rationale")).toHaveTextContent(
    "cover 3000x3000 -> 1200x1200; original kept as cover-hires.jpg",
  );
});

test("COVERS_RESIZE_6 a plain library move shows no rationale line", async () => {
  mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [queueItem(changeset())],
  });
  renderApp("/review?profile=main");
  await screen.findByText("Library move");
  expect(screen.queryByTestId("file-changeset-rationale")).toBeNull();
});

test("REVIEW_QUEUE_22 the move card offers Back to the earlier gate", async () => {
  const { calls } = mockApi({
    "GET /api/profiles": () => PROFILES,
    "GET /api/review": () => [
      {
        busy: false,
        kind: "changeset",
        id: "cs1",
        profile: "main",
        run_id: RUN,
        workflow: "into-library",
        step_id: "into-library",
        created_at: T0,
        changeset: changeset(),
        state: "proposed",
        back_to: "final-write",
        back_album: "album-1",
      } satisfies S["QueueItem"],
    ],
    [`POST /api/runs/${RUN}/lanes/album-1/back`]: () => ({
      key: "album-1",
      label: "Abbey Road",
      state: "active",
      position: "final-write",
      holds: [],
      steps: {},
    }),
  });
  renderApp("/review?profile=main");
  const card = await screen.findByTestId("file-changeset");
  const item = card.closest<HTMLElement>('[data-testid="queue-item"]') as HTMLElement;
  const actions = within(item).getByTestId("item-actions");
  expect(within(actions).getByRole("button", { name: "Reject" })).toBeInTheDocument();
  fireEvent.click(within(actions).getByRole("button", { name: "Back to final-write" }));
  await waitFor(() => {
    const posted = calls.filter((c: Call) => c.path === `/api/runs/${RUN}/lanes/album-1/back`);
    expect(posted.map((c) => c.body)).toEqual([{ to_step: "final-write" }]);
  });
});
