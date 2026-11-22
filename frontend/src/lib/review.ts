import type { ChangeSet, CurrentTarget, LaneOut, QueueItem, TargetRef } from "../api/types";

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

// -- step sections (F-REVIEW-QUEUE REVIEW-QUEUE-11, F-UI-REFRESH UI-REFRESH-13) ----------

export interface StepSection {
  /** `workflow` + NUL + `step id`. */
  key: string;
  workflow: string | null;
  stepId: string | null;
  label: string;
  /** Oldest first: a new item joins the bottom. */
  items: QueueItem[];
}

const byTime = (a: QueueItem, b: QueueItem) =>
  a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : a.id < b.id ? -1 : 1;

/**
 * Group queue items into one section per workflow step (REVIEW-QUEUE-11). Sections follow
 * the workflow's step order (`stepOrders`, workflow name to step ids); steps not in it come
 * last, by oldest item. Items in `decided` were just decided and are left out (UI-REFRESH-20);
 * a section with nothing left is dropped.
 */
export function groupByStep(
  items: readonly QueueItem[],
  stepOrders: ReadonlyMap<string, readonly string[]>,
  decided: ReadonlySet<string> = new Set(),
): StepSection[] {
  const sections = new Map<string, StepSection>();
  for (const item of items) {
    if (decided.has(item.id)) continue;
    const workflow = item.workflow ?? null;
    const stepId = item.step_id ?? null;
    const key = `${workflow ?? ""}\u0000${stepId ?? ""}`;
    let sec = sections.get(key);
    if (sec === undefined) {
      sec = { key, workflow, stepId, label: stepId ?? "Other", items: [] };
      sections.set(key, sec);
    }
    sec.items.push(item);
  }
  const all = [...sections.values()];
  for (const sec of all) sec.items.sort(byTime);
  const workflows = new Set(all.map((sec) => sec.workflow));
  if (workflows.size > 1) {
    for (const sec of all) if (sec.workflow !== null) sec.label = `${sec.workflow} · ${sec.label}`;
  }
  const index = (sec: StepSection) => {
    const order = sec.workflow === null ? undefined : stepOrders.get(sec.workflow);
    const i = order && sec.stepId !== null ? order.indexOf(sec.stepId) : -1;
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  const oldest = (sec: StepSection) => (sec.items[0] as QueueItem).created_at;
  return all.sort(
    (a, b) =>
      (a.workflow ?? "\uffff").localeCompare(b.workflow ?? "\uffff") ||
      index(a) - index(b) ||
      (oldest(a) < oldest(b) ? -1 : oldest(a) > oldest(b) ? 1 : 0),
  );
}

/** The album lane holding a queue item, if exactly one does (C-LANES). */
export function laneOf(item: QueueItem, lanes: readonly LaneOut[]): LaneOut | null {
  const held = lanes.filter((lane) => lane.holds.some((h) => h.ref === item.id));
  return held.length === 1 ? (held[0] as LaneOut) : null;
}
