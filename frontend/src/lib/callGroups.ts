import type { StepState } from "../api/types";

/** A row of the run page's step table: a step, or the header of a workflow call. */
export type StepRow =
  | { kind: "step"; step: StepState; depth: number }
  | { kind: "call"; id: string; depth: number; label: string; workflow: string | null };

/**
 * Called steps grouped under their call (S-SUBWORKFLOW SUBWORKFLOW-10): a header row
 * each time a call starts, then its steps indented one level deeper.
 */
export function stepRows(steps: StepState[]): StepRow[] {
  const rows: StepRow[] = [];
  let prev: string[] = [];
  for (const step of steps) {
    const path = step.call_path ?? [];
    let same = 0;
    while (same < path.length && same < prev.length && path[same] === prev[same]) same += 1;
    for (let d = same; d < path.length; d += 1) {
      const innermost = d === path.length - 1;
      rows.push({
        kind: "call",
        id: path.slice(0, d + 1).join("/"),
        depth: d,
        label: (innermost ? step.workflow_name : null) ?? path[d] ?? "",
        workflow: innermost ? (step.workflow ?? null) : null,
      });
    }
    rows.push({ kind: "step", step, depth: path.length });
    prev = path;
  }
  return rows;
}
