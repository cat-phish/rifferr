import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { groupChanges } from "./gateChanges";

type S = components["schemas"];

function change(
  scope: "album" | "item",
  id: string,
  field: string,
  before: S["GateChange"]["before"],
  after: S["GateChange"]["after"],
  steps = ["dates"],
): S["GateChange"] {
  return { target: { scope, id }, field, before, after, existed_before: true, steps };
}

function album(changes: S["GateChange"][], tracks = 15): S["GateAlbum"] {
  const items = Object.fromEntries(
    Array.from({ length: tracks }, (_, n) => [
      `item-${String(n + 1)}`,
      `${String(n + 1)}. Song ${String(n + 1)}`,
    ]),
  );
  return {
    target: { scope: "album", id: "a1" },
    label: "A - B",
    changes,
    items,
    imported_by_run: true,
  };
}

const all = (field: string, before: number, after: number, n = 15) => [
  change("album", "a1", field, before, after),
  ...Array.from({ length: n }, (_, i) =>
    change("item", `item-${String(i + 1)}`, field, before, after),
  ),
];

test("REVIEW_4 an album change propagated to every track is one row with its step", () => {
  const rows = groupChanges(album([...all("original_year", 0, 1982), ...all("year", 0, 1982)]));
  expect(rows).toHaveLength(2);
  const [first] = rows;
  expect(first?.field).toBe("original_year");
  expect(first?.where).toBe("album and all 15 tracks");
  expect(first?.steps).toEqual(["dates"]);
  expect(first?.tracks[0]).toBe("1. Song 1");
});

test("REVIEW_4 some tracks are named by number ranges; one by its label", () => {
  const some = ["item-1", "item-2", "item-3", "item-4", "item-7"].map((id) =>
    change("item", id, "title", "x", "y", ["suffixes"]),
  );
  const [row] = groupChanges(album(some));
  expect(row?.where).toBe("tracks 1–4, 7");
  const [one] = groupChanges(album([change("item", "item-3", "title", "x", "y")]));
  expect(one?.where).toBe("3. Song 3");
  const [two] = groupChanges(
    album([change("item", "item-1", "t", "x", "y"), change("item", "item-2", "t", "x", "y")]),
  );
  expect(two?.where).toBe("tracks 1, 2");
});

test("REVIEW_4 different steps or values stay separate rows", () => {
  const rows = groupChanges(
    album([
      change("album", "a1", "genres", "Rock", "Punk", ["genre-bucket"]),
      change("item", "item-1", "genres", "Rock", "Punk", ["subgenres"]),
      change("item", "item-2", "genres", "Rock", "Oi", ["genre-bucket"]),
    ]),
  );
  expect(rows.map((r) => r.where)).toEqual(["album", "1. Song 1", "2. Song 2"]);
});

test("REVIEW_4 a change to beets' date fields reads as one date", () => {
  const a = {
    ...album(
      [
        change("album", "a1", "day", 29, 9),
        change("item", "item-1", "day", 29, 9),
        change("album", "a1", "releasedate", null, "2018-06-29"),
      ],
      1,
    ),
    fields: { year: 2018, month: 6, day: 9 },
  };
  const rows = groupChanges(a);
  expect(rows.map((r) => r.label ?? r.field)).toEqual(["date", "releasedate"]);
  const [date] = rows;
  expect([date?.before, date?.after]).toEqual(["2018-06-29", "2018-06-09"]);
  expect(date?.fields).toEqual(["day"]);
  expect(date?.where).toBe("album and 1. Song 1");
});

test("REVIEW_4 a date change on tracks only stays its own row", () => {
  const rows = groupChanges(album([change("item", "item-1", "day", 29, 9)], 2));
  expect(rows.map((r) => [r.label, r.field])).toEqual([[undefined, "day"]]);
});

test("REVIEW_4 without the album's fields a partial date change stays as it is", () => {
  const rows = groupChanges(album([change("album", "a1", "day", 29, 9)], 1));
  expect(rows.map((r) => [r.label, r.field])).toEqual([[undefined, "day"]]);
});
