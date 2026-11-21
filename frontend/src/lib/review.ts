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

// -- album blocks (F-UI-REFRESH UI-REFRESH-13 .. -15) ---------------------------------------

/** Lane states after which a lane does nothing more (C-LANES). */
const FINAL_LANE_STATES: ReadonlySet<string> = new Set([
  "done",
  "skipped",
  "failed",
  "rejected",
  "gone",
  "cancelled",
]);

export interface AlbumBlock {
  /** Lane key; `"*several"` for items several lanes hold, `"*other"` for items none holds. */
  key: string;
  label: string;
  lane: LaneOut | null;
  /** Items that wait for the user. */
  items: QueueItem[];
  /** Items the user decided whose result hasn't come back yet (UI-REFRESH-20). */
  decided: QueueItem[];
}

export interface RunBlocks {
  /** Albums with something to decide, first come first: an album whose next item
   * arrives joins the bottom (UI-REFRESH-13). */
  waiting: AlbumBlock[];
  /** Albums still working: running a step, or busy with a decision (UI-REFRESH-20). */
  working: AlbumBlock[];
}

/**
 * Group one run's queue items by album lane (UI-REFRESH-13, -20). Items in `decided`
 * left the waiting list the moment the user decided them. Items held by several lanes
 * (a gate over several albums) and items no lane holds form blocks of their own.
 */
export function groupByLane(
  items: QueueItem[],
  lanes: readonly LaneOut[],
  stepOrder: readonly string[],
  decided: ReadonlySet<string> = new Set(),
): RunBlocks {
  const holders = new Map<string, LaneOut[]>();
  for (const lane of lanes) {
    for (const hold of lane.holds) {
      const list = holders.get(hold.ref) ?? [];
      list.push(lane);
      holders.set(hold.ref, list);
    }
  }
  const blocks = new Map<string, AlbumBlock>();
  const block = (key: string, label: string, lane: LaneOut | null): AlbumBlock => {
    let b = blocks.get(key);
    if (b === undefined) {
      b = { key, label, lane, items: [], decided: [] };
      blocks.set(key, b);
    }
    return b;
  };
  for (const lane of lanes) {
    if (!FINAL_LANE_STATES.has(lane.state)) block(lane.key, lane.label, lane);
  }
  for (const item of items) {
    const held = holders.get(item.id) ?? [];
    const b =
      held.length === 1
        ? block((held[0] as LaneOut).key, (held[0] as LaneOut).label, held[0] as LaneOut)
        : held.length > 1
          ? block("*several", "Several albums", null)
          : block("*other", "Other", null);
    (decided.has(item.id) ? b.decided : b.items).push(item);
  }
  const position = (step: string | null | undefined) => {
    const i = step ? stepOrder.indexOf(step) : -1;
    return i < 0 ? stepOrder.length : i;
  };
  const byTime = (a: QueueItem, b: QueueItem) =>
    a.created_at < b.created_at ? -1 : a.created_at > b.created_at ? 1 : 0;
  const all = [...blocks.values()];
  for (const b of all) {
    b.items.sort((x, y) => position(x.step_id) - position(y.step_id) || byTime(x, y));
  }
  const oldest = (b: AlbumBlock) =>
    b.items.reduce((t, i) => (i.created_at < t ? i.created_at : t), "\uffff");
  return {
    waiting: all
      .filter((b) => b.items.length > 0)
      .sort((x, y) => (oldest(x) < oldest(y) ? -1 : oldest(x) > oldest(y) ? 1 : 0)),
    working: all.filter((b) => b.items.length === 0),
  };
}

export type LaneStatusToken = "warning" | "success" | "danger" | null;

/** A block's status line (UI-REFRESH-14). */
export function laneStatus(block: AlbumBlock): { text: string; token: LaneStatusToken } {
  const lane = block.lane;
  if (block.items.length > 0) return { text: "waiting on you", token: "warning" };
  if (lane?.state === "active") {
    return { text: lane.position ? `running ${lane.position}` : "running", token: null };
  }
  if (block.decided.length > 0) return { text: "working on your decision", token: null };
  if (lane === null) return { text: "", token: null };
  if (lane.state === "waiting") return { text: "waiting", token: null };
  if (lane.state === "done") return { text: "done", token: "success" };
  if (lane.state === "failed") return { text: "failed", token: "danger" };
  return { text: lane.state, token: null };
}

/** "2 waiting · 1 active · 5 done" (UI-REFRESH-15). */
export function laneCounts(lanes: readonly LaneOut[]): string {
  const counts = new Map<string, number>();
  for (const lane of lanes) counts.set(lane.state, (counts.get(lane.state) ?? 0) + 1);
  const order = ["waiting", "active", "done", "skipped", "failed", "rejected", "cancelled", "gone"];
  return order
    .filter((s) => counts.has(s))
    .map((s) => `${String(counts.get(s))} ${s}`)
    .join(" · ");
}
