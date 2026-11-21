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
