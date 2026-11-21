import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ok } from "../api/client";
import type { RunEvent } from "../api/types";
import { useEventStream } from "./useEventStream";

/** Events the app follows on ``/api/events``: run-level ones for the runs list
 * (UI-SHELL-9), the ones that change the review queue (REVIEW-QUEUE-10) and the
 * trigger events (F-TRIGGERS TRIGGERS-24). */
export const APP_EVENTS = [
  "run.started",
  "run.waiting_review",
  "run.resumed",
  "run.finished",
  "review.requested",
  "review.resolved",
  "review.decided",
  "import.requested",
  "import.resolved",
  "cleanup.requested",
  "cleanup.resolved",
  "move.requested",
  "move.resolved",
  "changeset.proposed",
  "changeset.applied",
  "changeset.rejected",
  "changeset.conflicted",
  "changeset.undone",
  "job.finished",
  // An album's lane and step progress, for the queue's album blocks (UI-REFRESH-14).
  "lane.started",
  "lane.waiting",
  "lane.resumed",
  "lane.finished",
  "run.step.started",
  "run.step.finished",
  "trigger.fired",
  "trigger.skipped",
  "trigger.error",
  "inbox.changed",
] as const;

/** What waits for a decision in ``profile`` (REVIEW-QUEUE-1). */
export function useReviewQueue(profile: string | null) {
  return useQuery({
    queryKey: ["review", profile],
    enabled: profile !== null,
    queryFn: async () =>
      ok(await api.GET("/api/review", { params: { query: { profile: profile ?? undefined } } })),
  });
}

/** Keep the runs list and the review queue (and the ChangeSets shown in it) current from
 * ``/api/events``. Mounted once, by the app shell, so the app holds one connection. */
export function useAppEvents(onEvent?: (event: RunEvent) => void): void {
  const queryClient = useQueryClient();
  useEventStream<RunEvent>("/api/events", {
    types: APP_EVENTS,
    onEvent: (_type, event) => {
      // The review queue's album blocks follow their run's lanes (UI-REFRESH-14).
      if (event.type.startsWith("lane.") || event.type.startsWith("run.")) {
        void queryClient.invalidateQueries({ queryKey: ["run", event.run_id] });
      }
      if (event.type.startsWith("lane.") || event.type.startsWith("run.step.")) {
        onEvent?.(event);
        return;
      }
      if (event.type.startsWith("run.") || event.type === "trigger.fired") {
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
      }
      // The dashboard's inbox and recent imports (F-DASHBOARD DASHBOARD-15).
      if (
        event.type.startsWith("run.") ||
        event.type.startsWith("trigger.") ||
        event.type.startsWith("inbox.") ||
        event.type === "job.finished" // an import undo marks its input undone
      ) {
        void queryClient.invalidateQueries({ queryKey: ["inbox"] });
      }
      if (event.type === "run.finished" || event.type === "job.finished") {
        void queryClient.invalidateQueries({ queryKey: ["imports"] });
      }
      if (event.type.startsWith("inbox.")) return;
      if (event.type.startsWith("trigger.")) {
        void queryClient.invalidateQueries({ queryKey: ["triggers"] });
        return;
      }
      void queryClient.invalidateQueries({ queryKey: ["review"] });
      if (event.type.startsWith("changeset.")) {
        void queryClient.invalidateQueries({ queryKey: ["changeset-current"] });
      }
      onEvent?.(event);
    },
  });
}
