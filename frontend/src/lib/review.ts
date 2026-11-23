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

/** The route name of items with no known workflow ("Other"). */
export const OTHER_WORKFLOW = "~other";

/** A workflow's page (UI-REFRESH-23), with optional `stage`, `item`, `run` or `tab`. */
export function workflowLink(
  profile: string | null,
  name: string,
  query: Record<string, string | null | undefined> = {},
): string {
  const params = new URLSearchParams();
  if (profile !== null) params.set("profile", profile);
  for (const [k, v] of Object.entries(query)) if (v !== null && v !== undefined) params.set(k, v);
  const q = params.toString();
  return `/workflows/${encodeURIComponent(name === "" ? OTHER_WORKFLOW : name)}${q ? `?${q}` : ""}`;
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

/** A workflow the review page offers (UI-REFRESH-22): one that has a step that can wait,
 * or that has items waiting. `name` is "" for items with no workflow. */
export interface ReviewWorkflow {
  name: string;
  /** Every step id, in run order (calls expanded). */
  steps: readonly string[];
  /** The steps that can put something in the queue (`may_wait`); each gets a tab. */
  waits: readonly string[];
  /** Items waiting in it. */
  count: number;
}

interface WorkflowSteps {
  name: string;
  steps?: readonly { id: string; may_wait: boolean }[];
}

/** The profile's workflows with their counts (UI-REFRESH-22): those that can wait, or every
 * one with `all` (the Workflows hub, UI-REFRESH-23), plus any other with waiting items. */
export function reviewWorkflows(
  details: readonly WorkflowSteps[],
  items: readonly QueueItem[],
  all = false,
): ReviewWorkflow[] {
  const counts = new Map<string, number>();
  for (const i of items) counts.set(i.workflow ?? "", (counts.get(i.workflow ?? "") ?? 0) + 1);
  const out = details
    .map((d) => ({
      name: d.name,
      steps: (d.steps ?? []).map((s) => s.id),
      waits: (d.steps ?? []).filter((s) => s.may_wait).map((s) => s.id),
      count: counts.get(d.name) ?? 0,
    }))
    .filter((w) => all || w.waits.length > 0 || w.count > 0);
  for (const [name, count] of counts)
    if (!out.some((w) => w.name === name)) out.push({ name, steps: [], waits: [], count });
  return out;
}

/** A stage tab of the review page (UI-REFRESH-13). */
export interface StageTab {
  /** Its `?stage=` value: the step id, or {@link OTHER_STAGE}. */
  value: string;
  label: string;
  /** Its waiting items; undefined while none wait. */
  section: StepSection | undefined;
}

export const OTHER_STAGE = "~other";

/** One tab per step of `wf` that can wait (every step with `all`, the Pipeline tab of
 * UI-REFRESH-24), whether or not anything waits there, plus one per other step that has
 * items, in run order; items with no step go last ("Other"). `sections` are this
 * workflow's (from {@link groupByStep}). */
export function stageTabs(
  wf: ReviewWorkflow,
  sections: readonly StepSection[],
  all = false,
): StageTab[] {
  const ids = all ? [...new Set(wf.steps)] : wf.waits;
  const tabs: StageTab[] = ids.map((id) => ({
    value: id,
    label: id,
    section: sections.find((s) => s.stepId === id),
  }));
  for (const sec of sections)
    if (!tabs.some((t) => t.section === sec))
      tabs.push({ value: sec.stepId ?? OTHER_STAGE, label: sec.stepId ?? "Other", section: sec });
  const index = (t: StageTab) => {
    const i = t.value === OTHER_STAGE ? -1 : wf.steps.indexOf(t.value);
    return i < 0 ? Number.MAX_SAFE_INTEGER : i;
  };
  return tabs.sort((a, b) => index(a) - index(b));
}

/** The Pipeline tab's trailing tab: live lanes past the last step (UI-REFRESH-24). */
export const FINISHING_STAGE = "~finishing";

/** An album (or a run with no lanes yet) at a step, not waiting on review (UI-REFRESH-24). */
export interface PipelineEntry {
  key: string;
  runId: string;
  label: string;
  /** Lane state, or the run's state for a run with no lanes yet. */
  state: string;
  /** When it reached the step. */
  since: string;
  starting: boolean;
}

/** Live lanes and starting runs by the step they are at (UI-REFRESH-24): a lane held by a
 * shown queue item (`waiting`) is left out, as it is already listed above them; a lane at
 * no known step goes to {@link FINISHING_STAGE}; starting runs go to the first step. */
export function pipelineByStep(
  steps: readonly string[],
  lanes: readonly (LaneOut & { run_id: string; updated_at: string })[],
  starting: readonly { run_id: string; state: string; input?: string | null; created_at: string }[],
  waiting: ReadonlySet<string>,
): Map<string, PipelineEntry[]> {
  const out = new Map<string, PipelineEntry[]>();
  const add = (step: string, e: PipelineEntry) => {
    out.set(step, [...(out.get(step) ?? []), e]);
  };
  const first = steps[0] ?? FINISHING_STAGE;
  for (const r of starting)
    add(first, {
      key: `run:${r.run_id}`,
      runId: r.run_id,
      label: `Run starting${r.input ? `: ${folderName(r.input)}` : ""}`,
      state: r.state,
      since: r.created_at,
      starting: true,
    });
  for (const lane of lanes) {
    if (lane.holds.some((h) => waiting.has(h.ref))) continue;
    const step =
      lane.position !== null && steps.includes(lane.position) ? lane.position : FINISHING_STAGE;
    add(step, {
      key: `${lane.run_id}:${lane.key}`,
      runId: lane.run_id,
      label: lane.label,
      state: lane.state,
      since: lane.updated_at,
      starting: false,
    });
  }
  return out;
}

function folderName(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() ?? path;
}
