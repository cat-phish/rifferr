import type { components } from "../api/schema";

type S = components["schemas"];
export type AlbumSnapshot = S["AlbumSnapshot"];
export type ItemSnapshot = S["ItemSnapshot"];
export type TargetRef = S["TargetRef"];
export type ManualChange = S["ManualChange"];
export type FieldValue = boolean | number | string | string[] | null;
export type Scope = "album" | "item";

/** Built-in default columns: beets' own field names (TAG-GRID-3, open question 1). */
export const DEFAULT_COLUMNS: Record<Scope, string[]> = {
  album: ["albumartist", "album", "year", "albumtype", "genre"],
  item: ["track", "title", "artist", "album", "length"],
};
export const DEFAULT_WIDTH = 160;
export const MIN_WIDTH = 60;

/** One album or item the grid shows, with the album it belongs to. */
export interface Row {
  key: string;
  target: TargetRef;
  album: string;
  fields: Record<string, FieldValue>;
}

export const rowKey = (t: TargetRef): string => `${t.scope}:${t.id}`;
export const cellKey = (row: string, field: string): string => `${row}\u0000${field}`;

export function albumRow(a: AlbumSnapshot): Row {
  const target = { scope: "album" as const, id: a.id };
  return { key: rowKey(target), target, album: a.id, fields: a.fields };
}

export function itemRow(a: AlbumSnapshot, i: ItemSnapshot): Row {
  const target = { scope: "item" as const, id: i.id };
  return { key: rowKey(target), target, album: a.id, fields: i.fields };
}

/** A field value as text. Lists show their entries joined, for reading only (TAG-GRID-14). */
export function show(value: FieldValue | undefined): string {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.join("; ");
  return String(value);
}

/** Fields holding a list anywhere on the page: edited as lists (TAG-GRID-5). */
export function listFields(albums: AlbumSnapshot[]): Set<string> {
  const out = new Set<string>();
  const add = (fields: Record<string, FieldValue>) => {
    for (const [k, v] of Object.entries(fields)) if (Array.isArray(v)) out.add(k);
  };
  for (const a of albums) {
    add(a.fields);
    for (const i of a.items ?? []) add(i.fields);
  }
  return out;
}

/** A pending edit of one field: ``old`` is the value the user saw (``null`` = absent). */
export interface Edit {
  op: "set" | "delete";
  old: FieldValue;
  new: FieldValue;
}

export interface PendingTarget {
  target: TargetRef;
  album: string;
  changes: Record<string, Edit>;
}

/** Pending edits by row key; nothing reaches the library until Apply (TAG-GRID-5). */
export type Pending = Record<string, PendingTarget>;

function same(a: FieldValue, b: FieldValue): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }
  return a === b;
}

/**
 * ``pending`` with ``edit`` for ``field`` of ``row``. An edit back to the value the user
 * saw removes the pending edit instead. Typed text that reads like the current value
 * (``2001`` for the number 2001) counts as unchanged.
 */
export function withEdit(pending: Pending, row: Row, field: string, edit: Edit | null): Pending {
  const current = row.fields[field] ?? null;
  const unchanged =
    edit === null ||
    (edit.op === "delete" && current === null) ||
    (edit.op === "set" &&
      current !== null &&
      (same(edit.new, current) ||
        (typeof edit.new === "string" && !Array.isArray(current) && edit.new === show(current))));
  const others = Object.entries(pending[row.key]?.changes ?? {}).filter(([f]) => f !== field);
  const changes = Object.fromEntries(unchanged ? others : [...others, [field, edit]]);
  const rest = Object.entries(pending).filter(([k]) => k !== row.key);
  if (Object.keys(changes).length === 0) return Object.fromEntries(rest);
  return {
    ...Object.fromEntries(rest),
    [row.key]: { target: row.target, album: row.album, changes },
  };
}

/** The value a set edit would store for ``field`` on ``row``: lists for list fields. */
export function setEdit(row: Row, field: string, text: string, lists: Set<string>): Edit {
  const old = row.fields[field] ?? null;
  const isList = Array.isArray(old) || lists.has(field);
  return { op: "set", old, new: isList ? (text === "" ? [] : [text]) : text };
}

export function deleteEdit(row: Row, field: string): Edit {
  return { op: "delete", old: row.fields[field] ?? null, new: null };
}

/** The request body for check and apply (TAG-GRID-7, -8). */
export function toRequest(pending: Pending): ManualChange[] {
  return Object.values(pending).map((p) => ({
    target: p.target,
    album: p.album,
    changes: Object.entries(p.changes).map(([field, e]) =>
      e.op === "delete"
        ? { field, op: "delete" as const, old: e.old }
        : { field, op: "set" as const, old: e.old, new: e.new },
    ),
  }));
}

/** ``"album 01ABC"`` (a ``TargetRef.label()``) back to a row key. */
export function labelKey(label: string): string {
  const space = label.indexOf(" ");
  return space < 0 ? label : `${label.slice(0, space)}:${label.slice(space + 1)}`;
}

/** Problems from check, by cell; ``""`` field = the whole row. */
export function problemsByCell(
  problems: { target: TargetRef; field: string; message: string }[],
): Map<string, string> {
  const out = new Map<string, string>();
  for (const p of problems) {
    const key = cellKey(rowKey(p.target), p.field);
    out.set(key, out.has(key) ? `${out.get(key) ?? ""}; ${p.message}` : p.message);
  }
  return out;
}

// -- remembered columns (TAG-GRID-3) -------------------------------------------------

export interface ColumnPrefs {
  columns: string[];
  widths: Record<string, number>;
}

const prefsKey = (profile: string, scope: Scope) => `rifferr.grid.${profile}.${scope}`;

/** Remembered per browser and profile; never written to the profile file. */
export function loadPrefs(profile: string, scope: Scope, fallback: string[]): ColumnPrefs {
  try {
    const raw = window.localStorage.getItem(prefsKey(profile, scope));
    if (raw !== null) {
      const parsed = JSON.parse(raw) as Partial<ColumnPrefs>;
      if (Array.isArray(parsed.columns) && parsed.columns.every((c) => typeof c === "string")) {
        return { columns: parsed.columns, widths: parsed.widths ?? {} };
      }
    }
  } catch {
    // storage blocked or corrupt: use the default
  }
  return { columns: fallback, widths: {} };
}

export function savePrefs(profile: string, scope: Scope, prefs: ColumnPrefs): void {
  try {
    window.localStorage.setItem(prefsKey(profile, scope), JSON.stringify(prefs));
  } catch {
    // not remembered; still works for this page
  }
}
