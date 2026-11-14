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
  "import.requested",
  "import.resolved",
  "changeset.proposed",
  "changeset.applied",
  "changeset.rejected",
  "changeset.conflicted",
  "changeset.undone",
  "job.finished",
  "trigger.fired",
  "trigger.skipped",
  "trigger.error",
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
      if (event.type.startsWith("run.") || event.type === "trigger.fired") {
        void queryClient.invalidateQueries({ queryKey: ["runs"] });
      }
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
