import type { LaneHold, LaneOut, RunDetail, RunEvent, StepState } from "../api/types";

export type StepStatus = StepState["status"];
export type Stream = "stdout" | "stderr" | "info";

export interface LogLine {
  seq: number;
  ts: string;
  step: string | null;
  stream: Stream;
  text: string;
  target?: string;
}

export interface RunView {
  lastSeq: number;
  status: string;
  error: string | null;
  steps: StepState[];
  /** One album's path through the run, live from ``lane.*`` events (LANES-15). */
  lanes: LaneOut[];
  log: LogLine[];
}

export function initialView(run: RunDetail): RunView {
  return {
    lastSeq: 0,
    status: run.state,
    error: run.error,
    steps: run.steps.map((s) => ({ ...s, status: "pending" })),
    lanes: run.lanes ?? [],
    log: [],
  };
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
export const isTerminal = (status: string): boolean => TERMINAL.has(status);

function str(v: unknown): string | undefined {
  return typeof v === "string" ? v : undefined;
}

/** Fold one event into the view; events already applied (by seq) are ignored (UI-SHELL-7). */
export function applyEvent(view: RunView, e: RunEvent): RunView {
  if (e.seq <= view.lastSeq) return view;
  const next: RunView = { ...view, lastSeq: e.seq };
  const setStep = (patch: Partial<StepState>): void => {
    next.steps = view.steps.map((s) => (s.id === e.step_id ? { ...s, ...patch } : s));
  };
  switch (e.type) {
    case "run.started":
    case "run.resumed":
      next.status = "running";
      break;
    case "run.waiting_review":
      next.status = "waiting_review";
      break;
    case "run.step.started":
      setStep({ status: "running" });
      break;
    case "run.step.finished": {
      setStep({
        status: (str(e.data.status) ?? "failed") as StepStatus,
        error: str(e.data.error) ?? null,
      });
      const error = str(e.data.error);
      if (error !== undefined) {
        next.log = [...view.log, line(e, "stderr", error)];
      }
      break;
    }
    case "run.step.log": {
      const stream = str(e.data.stream);
      const kind: Stream = stream === "stdout" || stream === "stderr" ? stream : "info";
      next.log = [...view.log, line(e, kind, str(e.data.line) ?? "", str(e.data.target))];
      break;
    }
    case "lane.started":
    case "lane.waiting":
    case "lane.resumed":
    case "lane.finished":
      next.lanes = upsertLane(view.lanes, e.data);
      break;
    case "run.finished": {
      next.status = str(e.data.status) ?? "failed";
      next.error = str(e.data.error) ?? null;
      // Steps that never ran are skipped; a step still running was cut off.
      next.steps = view.steps.map((s) =>
        s.status === "pending"
          ? { ...s, status: "skipped" }
          : s.status === "running"
            ? { ...s, status: "cancelled" }
            : s,
      );
      break;
    }
  }
  return next;
}

const LANE_STATES = new Set<string>([
  "active",
  "waiting",
  "done",
  "failed",
  "rejected",
  "gone",
  "cancelled",
]);

function upsertLane(lanes: LaneOut[], data: Record<string, unknown>): LaneOut[] {
  const key = str(data.key);
  const state = str(data.state);
  if (key === undefined || state === undefined || !LANE_STATES.has(state)) return lanes;
  const holds = Array.isArray(data.holds) ? (data.holds as LaneHold[]) : [];
  const position = str(data.position) ?? null;
  const known = lanes.find((l) => l.key === key);
  const patch = { state: state as LaneOut["state"], position, holds };
  if (known === undefined) {
    return [...lanes, { key, label: str(data.label) ?? key, steps: {}, ...patch }];
  }
  return lanes.map((l) => (l.key === key ? { ...l, ...patch } : l));
}

function line(e: RunEvent, stream: Stream, text: string, target?: string): LogLine {
  const l: LogLine = { seq: e.seq, ts: e.ts, step: e.step_id ?? null, stream, text };
  if (target !== undefined) l.target = target;
  return l;
}
