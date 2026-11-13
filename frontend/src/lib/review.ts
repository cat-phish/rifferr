import type { ChangeSet, CurrentTarget, QueueItem, TargetRef } from "../api/types";

export type Value = boolean | number | string | string[] | null | undefined;

/** Values as plain text (REVIEW-QUEUE-17): lists joined with "; ", absent as an em dash. */
export function formatValue(v: Value): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.join("; ");
  return String(v);
}

/** Same value, as the engine compares them (lists in order). Absent and null are equal. */
export function sameValue(a: Value, b: Value): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

export const targetKey = (t: TargetRef): string => `${t.scope}:${t.id}`;

/** One field change with its index in the ChangeSet (CHANGESET-9). */
export interface FieldRow {
  index: number;
  target: TargetRef;
  field: string;
  op: "set" | "delete";
  old: Value;
  new: Value;
}

export function fieldRows(cs: ChangeSet): FieldRow[] {
  const rows: FieldRow[] = [];
  for (const t of cs.targets) {
    for (const c of t.changes) {
      rows.push({
        index: rows.length,
        target: t.target,
        field: c.field,
        op: c.op,
        old: c.old,
        new: c.op === "delete" ? null : c.new,
      });
    }
  }
  return rows;
}

/** The current value of a row's field: ``undefined`` while unknown, ``null`` if absent. */
export function currentOf(row: FieldRow, current: CurrentTarget[] | undefined): Value {
  if (current === undefined) return undefined;
  const t = current.find((c) => targetKey(c.target) === targetKey(row.target));
  if (t === undefined || t.missing) return undefined;
  return t.fields?.[row.field] ?? null;
}

export interface RunGroup {
  runId: string;
  profile: string;
  workflow: string | null;
  items: QueueItem[];
}

/** Queue items grouped by run, newest run first; items stay oldest first (REVIEW-QUEUE-11).
 * Run ids are ULIDs, so they sort by creation time. */
export function groupByRun(items: QueueItem[]): RunGroup[] {
  const groups = new Map<string, RunGroup>();
  for (const item of items) {
    let g = groups.get(item.run_id);
    if (g === undefined) {
      g = { runId: item.run_id, profile: item.profile, workflow: item.workflow ?? null, items: [] };
      groups.set(item.run_id, g);
    }
    g.items.push(item);
  }
  return [...groups.values()].sort((a, b) => (a.runId < b.runId ? 1 : a.runId > b.runId ? -1 : 0));
}

/** Plural helper for counts in messages. */
export function count(n: number, one: string, many = `${one}s`): string {
  return `${String(n)} ${n === 1 ? one : many}`;
}

/** A link into the queue: one item (REVIEW-QUEUE-16) or one run's items. */
export function reviewLink(profile: string | null, key: "item" | "run", value: string): string {
  const params = new URLSearchParams();
  if (profile !== null) params.set("profile", profile);
  params.set(key, value);
  return `/review?${params.toString()}`;
}
