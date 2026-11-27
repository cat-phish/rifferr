import { defaultAccepted, fieldRows } from "./review";
import { api, ok } from "../api/client";
import type { QueueItem } from "../api/types";

/** Queue items that can be decided together (F-DASHBOARD DASHBOARD-13). Import decisions
 * each need their own candidate choice, so they are left out. */
export type BulkKind = "changeset" | "gate" | "cleanup";

export interface BulkAction {
  value: string;
  label: string;
  danger: boolean;
}

export const BULK_ACTIONS: Record<BulkKind, BulkAction[]> = {
  changeset: [
    { value: "accept", label: "Accept", danger: false },
    { value: "reject", label: "Reject", danger: true },
  ],
  gate: [
    { value: "continue", label: "Continue", danger: false },
    { value: "reject", label: "Reject", danger: true },
  ],
  cleanup: [
    { value: "keep", label: "Keep", danger: false },
    { value: "delete", label: "Delete", danger: true },
  ],
};

export function bulkKind(item: QueueItem): BulkKind | null {
  if (item.kind === "changeset") return item.state === "proposed" ? "changeset" : null;
  if (item.kind === "gate" || item.kind === "cleanup") return item.kind;
  return null;
}

/** Decide one item as if alone (the same routes its card uses). */
async function decideOne(item: QueueItem, action: string): Promise<void> {
  if (item.kind === "changeset") {
    // Accepting leaves out changes with warnings, as the card does (AI-TAG-10).
    const rows = item.changeset ? fieldRows(item.changeset) : [];
    const keep = defaultAccepted(rows);
    if (action === "accept" && keep.size === 0 && rows.length > 0) return;
    const accepted = action === "accept" && keep.size < rows.length ? [...keep] : undefined;
    ok(
      await api.POST("/api/changesets/{changeset_id}/decision", {
        params: { path: { changeset_id: item.id } },
        body: { decision: action as "accept" | "reject", ...(accepted ? { accepted } : {}) },
      }),
    );
  } else if (item.kind === "gate") {
    ok(
      await api.POST("/api/reviews/{gate_id}/decision", {
        params: { path: { gate_id: item.id } },
        body: { decision: action as "continue" | "reject" },
      }),
    );
  } else {
    ok(
      await api.POST("/api/cleanups/{decision_id}/decision", {
        params: { path: { decision_id: item.id } },
        body: { decision: action as "delete" | "keep" },
      }),
    );
  }
}

export interface BulkFailure {
  id: string;
  message: string;
}

/** Decide every item, one after the other; returns the ones that failed. */
export async function decideAll(items: QueueItem[], action: string): Promise<BulkFailure[]> {
  const failed: BulkFailure[] = [];
  for (const item of items) {
    try {
      await decideOne(item, action);
    } catch (e) {
      failed.push({ id: item.id, message: e instanceof Error ? e.message : String(e) });
    }
  }
  return failed;
}
