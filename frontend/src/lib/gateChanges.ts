import type { GateAlbum } from "../api/types";

type Change = NonNullable<GateAlbum["changes"]>[number];

/** One row of a gate's "Changed by this run" list (S-REVIEW REVIEW-4): the same change,
 * made by the same steps, to the album and/or some of its tracks. */
export interface ChangeRow {
  key: string;
  field: string;
  before: Change["before"];
  after: Change["after"];
  existedBefore: boolean;
  steps: string[];
  album: boolean;
  /** Item ids, in the order the run's changes listed them. */
  items: string[];
  /** "album", "album and all 15 tracks", "tracks 1–4, 7", or one track's label. */
  where: string;
  /** Every changed track's label, for a tooltip. */
  tracks: string[];
  /** A name to show instead of the field, e.g. "date" for year/month/day. */
  label?: string;
  /** The fields behind a folded row, e.g. ["year", "day"]. */
  fields?: string[];
}

/** beets keeps a date as three fields; a change to them reads as one date (REVIEW-4). */
const DATES = [
  { label: "date", parts: ["year", "month", "day"] },
  { label: "original date", parts: ["original_year", "original_month", "original_day"] },
] as const;

export function groupChanges(album: GateAlbum): ChangeRow[] {
  const labels = album.items ?? {};
  const total = Object.keys(labels).length;
  const rows = new Map<string, Omit<ChangeRow, "where" | "tracks">>();
  for (const c of album.changes ?? []) {
    const steps = c.steps ?? [];
    const existed = c.existed_before;
    const key = JSON.stringify([c.field, existed ? c.before : null, existed, c.after, steps]);
    let row = rows.get(key);
    if (row === undefined) {
      row = {
        key,
        field: c.field,
        before: c.before,
        after: c.after,
        existedBefore: existed,
        steps,
        album: false,
        items: [],
      };
      rows.set(key, row);
    }
    if (c.target.scope === "album") row.album = true;
    else row.items.push(c.target.id);
  }
  const out = [...rows.values()].map((r) => {
    const tracks = r.items.map((id) => labels[id] ?? id);
    return { ...r, tracks, where: where(r.album, r.items, labels, total) };
  });
  return foldDates(out, album.fields ?? {});
}

/** Rows that change parts of one date on the same album and tracks, by the same steps,
 * become one row: "date 2018-06-29 → 2018-06-09". The album's fields fill in the parts
 * that didn't change, so only rows that include the album fold. */
function foldDates(rows: ChangeRow[], current: Record<string, Change["after"]>): ChangeRow[] {
  let out = rows;
  for (const { label, parts } of DATES) {
    const clusters = new Map<string, ChangeRow[]>();
    for (const r of out) {
      if (!r.album || !(parts as readonly string[]).includes(r.field)) continue;
      const key = JSON.stringify([r.items, r.steps]);
      clusters.set(key, [...(clusters.get(key) ?? []), r]);
    }
    for (const [key, cluster] of clusters) {
      const changed = new Map(cluster.map((r) => [r.field, r]));
      const value = (side: "before" | "after") =>
        ymd(
          parts.map((p) => {
            const r = changed.get(p);
            return r
              ? side === "before" && !r.existedBefore
                ? null
                : r[side]
              : (current[p] ?? null);
          }),
        );
      const first = cluster[0];
      if (first === undefined) continue;
      const row: ChangeRow = {
        ...first,
        key: `${label}/${key}`,
        label,
        fields: parts.filter((p) => changed.has(p)),
        before: value("before"),
        after: value("after"),
        existedBefore: true,
      };
      const index = out.indexOf(first);
      out = out.filter((r) => !cluster.includes(r));
      out.splice(Math.min(index, out.length), 0, row);
    }
  }
  return out;
}

/** "2018-06-29", "2018-06" or "2018"; null when there's no year. */
function ymd([y, m, d]: Change["after"][]): string | null {
  const n = (v: Change["after"] | undefined) => (typeof v === "number" && v > 0 ? v : 0);
  const [year, month, day] = [n(y), n(m), n(d)];
  if (year === 0) return null;
  const pad = (v: number) => String(v).padStart(2, "0");
  if (month === 0) return String(year);
  return day === 0 ? `${String(year)}-${pad(month)}` : `${String(year)}-${pad(month)}-${pad(day)}`;
}

function where(album: boolean, items: string[], labels: Record<string, string>, total: number) {
  const n = items.length;
  if (n === 0) return "album";
  const all = total > 0 && n === total;
  const tracks =
    all && n > 1
      ? `all ${String(n)} tracks`
      : n === 1
        ? (labels[items[0] ?? ""] ?? "1 track")
        : (ranges(items.map((id) => labels[id])) ?? `${String(n)} tracks`);
  return album ? `album and ${tracks}` : tracks;
}

/** "tracks 1–4, 7" from labels like "3. Title" or "2-3. Title"; null if any has no number. */
function ranges(labels: (string | undefined)[]): string | null {
  const numbers: string[] = [];
  for (const label of labels) {
    const m = label === undefined ? null : /^(\d+(?:-\d+)?)\. /.exec(label);
    if (m?.[1] === undefined) return null;
    numbers.push(m[1]);
  }
  if (numbers.some((n) => n.includes("-"))) return `tracks ${numbers.join(", ")}`;
  const sorted = [...new Set(numbers.map(Number))].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length;) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === (sorted[j] ?? 0) + 1) j++;
    const a = String(sorted[i]);
    const b = String(sorted[j]);
    parts.push(j - i >= 2 ? `${a}–${b}` : j > i ? `${a}, ${b}` : a);
    i = j + 1;
  }
  return `tracks ${parts.join(", ")}`;
}
