import { useEffect, useRef } from "react";
import type { RunEvent } from "../api/types";

type Listener = (event: RunEvent) => void;
const listeners = new Set<Listener>();

/** Hand an `/api/events` event to every page that listens (the shell holds the one
 * connection, `useAppEvents`). */
export function publishAppEvent(event: RunEvent): void {
  for (const fn of listeners) fn(event);
}

/** Listen to the app's events while mounted. */
export function useAppEventListener(fn: Listener): void {
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  useEffect(() => {
    const call: Listener = (e) => {
      ref.current(e);
    };
    listeners.add(call);
    return () => {
      listeners.delete(call);
    };
  }, []);
}
