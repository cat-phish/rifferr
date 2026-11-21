import { createContext, useCallback, useContext } from "react";

/** Marks a queue item as decided, so it leaves the waiting list at once
 * (F-UI-REFRESH UI-REFRESH-20). `jobId`: the job that carries the decision out. */
export const DecidedContext = createContext<(itemId: string, jobId?: string | null) => void>(
  () => undefined,
);

/** The queue item a card belongs to. */
export const ItemIdContext = createContext<string | null>(null);

/** In a card: mark the enclosing queue item decided once its decision is sent. */
export function useMarkDecided(): (jobId?: string | null) => void {
  const mark = useContext(DecidedContext);
  const id = useContext(ItemIdContext);
  return useCallback(
    (jobId?: string | null) => {
      if (id !== null) mark(id, jobId);
    },
    [mark, id],
  );
}

/** The job id in a decision response, if it has one. */
export function jobOf(response: unknown): string | null {
  if (typeof response === "object" && response !== null && "job_id" in response) {
    const id = response.job_id;
    return typeof id === "string" ? id : null;
  }
  return null;
}
