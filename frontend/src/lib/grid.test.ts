import { expect, test } from "vitest";
import {
  DEFAULT_COLUMNS,
  type Row,
  deleteEdit,
  labelKey,
  loadPrefs,
  problemsByCell,
  savePrefs,
  setEdit,
  toRequest,
  withEdit,
} from "./grid";

const row: Row = {
  key: "album:a1",
  target: { scope: "album", id: "a1" },
  album: "a1",
  fields: { year: 2001, genres: ["Rock"], album: "Live" },
};

test("TAG_GRID_5 an edit back to the value the user saw is no edit", () => {
  let p = withEdit({}, row, "year", setEdit(row, "year", "2002", new Set()));
  expect(p[row.key]?.changes.year).toEqual({ op: "set", old: 2001, new: "2002" });
  p = withEdit(p, row, "year", setEdit(row, "year", "2001", new Set()));
  expect(p).toEqual({});
  expect(withEdit({}, row, "label", deleteEdit(row, "label"))).toEqual({});
});

test("TAG_GRID_5 multi-value fields take a list, never split text", () => {
  expect(setEdit(row, "genres", "Rock; Pop", new Set()).new).toEqual(["Rock; Pop"]);
  expect(setEdit(row, "styles", "A", new Set(["styles"])).new).toEqual(["A"]);
  expect(setEdit(row, "album", "a; b", new Set()).new).toBe("a; b");
  const p = withEdit({}, row, "genres", { op: "set", old: ["Rock"], new: ["Rock"] });
  expect(p).toEqual({});
});

test("TAG_GRID_8 request body keeps old values and ops", () => {
  let p = withEdit({}, row, "year", setEdit(row, "year", "2002", new Set()));
  p = withEdit(p, row, "genres", deleteEdit(row, "genres"));
  expect(toRequest(p)).toEqual([
    {
      target: { scope: "album", id: "a1" },
      album: "a1",
      changes: [
        { field: "year", op: "set", old: 2001, new: "2002" },
        { field: "genres", op: "delete", old: ["Rock"] },
      ],
    },
  ]);
});

test("TAG_GRID_7 problems by cell and target labels", () => {
  const m = problemsByCell([
    { target: { scope: "item", id: "i1" }, field: "year", message: "a" },
    { target: { scope: "item", id: "i1" }, field: "year", message: "b" },
  ]);
  expect(m.get("item:i1\u0000year")).toBe("a; b");
  expect(labelKey("album 01ABC")).toBe("album:01ABC");
});

test("TAG_GRID_3 column choice is remembered per browser and profile", () => {
  window.localStorage.clear();
  expect(loadPrefs("main", "album", DEFAULT_COLUMNS.album).columns).toEqual(DEFAULT_COLUMNS.album);
  savePrefs("main", "album", { columns: ["album"], widths: { album: 99 } });
  expect(loadPrefs("main", "album", [])).toEqual({ columns: ["album"], widths: { album: 99 } });
  expect(loadPrefs("other", "album", ["x"]).columns).toEqual(["x"]);
  window.localStorage.setItem("rifferr.grid.main.item", "{not json");
  expect(loadPrefs("main", "item", ["y"]).columns).toEqual(["y"]);
});
