import { useEffect, useRef } from "react";
import { auth } from "../api/auth";

export interface StreamOptions<T> {
  /** Called once per message, in order. */
  onEvent: (type: string, data: T) => void;
  /** Close the EventSource after this event type (e.g. run.finished, API-8). */
  closeOn?: string;
  /** Event types to listen for; SSE messages with an ``event:`` name need explicit listeners. */
  types: readonly string[];
}

/**
 * Subscribe to an SSE stream with the browser's EventSource. Its automatic reconnect
 * sends ``Last-Event-ID``, so the server resumes after the last event (UI-SHELL-7);
 * callers still ignore duplicates by seq. ``url = null`` subscribes to nothing.
 */
export function useEventStream<T>(url: string | null, options: StreamOptions<T>): void {
  const opts = useRef(options);
  useEffect(() => {
    opts.current = options;
  });
  const typesKey = options.types.join(",");

  useEffect(() => {
    if (url === null) return;
    const source = new EventSource(url, { withCredentials: true });
    let closed = false;
    const handler = (ev: MessageEvent<string>): void => {
      if (closed) return;
      const data = JSON.parse(ev.data) as T;
      opts.current.onEvent(ev.type, data);
      if (ev.type === opts.current.closeOn) {
        closed = true;
        source.close();
      }
    };
    for (const type of typesKey.split(",")) source.addEventListener(type, handler);
    source.onerror = () => {
      // A closed stream with no reconnect usually means 401: check once.
      if (source.readyState === EventSource.CLOSED) {
        void fetch("/api/profiles", { credentials: "same-origin" }).then((r) => {
          if (r.status === 401) auth.markSignedOut();
        });
      }
    };
    return () => {
      closed = true;
      source.close();
    };
  }, [url, typesKey]);
}
