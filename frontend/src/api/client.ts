import createClient, { type Middleware } from "openapi-fetch";
import { auth } from "./auth";
import type { paths } from "./schema";
import type { ErrorEnvelope } from "./types";

/** Thrown for any non-2xx answer; carries the API error envelope (API-11). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, body: unknown) {
    const env = (body as Partial<ErrorEnvelope> | undefined)?.error;
    super(env?.message ?? `HTTP ${String(status)}`);
    this.status = status;
    this.code = env?.code ?? "http_error";
    this.details = env?.details;
  }
}

/**
 * A fresh `client` id and an increasing `seq` for one page's read-only worker requests
 * (previews, library queries). The server then supersedes by the order they were sent,
 * not the order they arrived, so an abandoned older request can't replace a newer one
 * (READ-ORDER-7, B-053).
 */
export class RequestOrder {
  readonly client: string;
  private last = -1;

  constructor(prefix: string) {
    this.client = `${prefix}-${Math.random().toString(36).slice(2)}`;
  }

  /** The next `seq`, one higher than the last. */
  next(): number {
    this.last += 1;
    return this.last;
  }
}

const signedOutOn401: Middleware = {
  onResponse({ response }) {
    if (response.status === 401) auth.markSignedOut();
    return response;
  },
};

/** Same origin, cookie auth, JSON only (SECURITY-3, -4). */
export const api = createClient<paths>({
  baseUrl: window.location.origin, // same origin; absolute so Request() accepts it
  credentials: "same-origin",
  headers: { "Content-Type": "application/json" },
  fetch: (request: Request) => globalThis.fetch(request),
});
api.use(signedOutOn401);

/** Unwrap an openapi-fetch result or throw ``ApiError``. */
export function ok<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data === undefined || !result.response.ok) {
    throw new ApiError(result.response.status, result.error);
  }
  return result.data;
}
