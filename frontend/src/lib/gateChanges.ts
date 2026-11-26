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
}

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
  return [...rows.values()].map((r) => {
    const tracks = r.items.map((id) => labels[id] ?? id);
    return { ...r, tracks, where: where(r.album, r.items, labels, total) };
  });
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
