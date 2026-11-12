import { render } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { vi } from "vitest";
import { auth } from "./api/auth";
import { App } from "./App";
import { Providers, newQueryClient } from "./providers";

type Json = unknown;

/** What the stubbed ``matchMedia`` reports for ``prefers-color-scheme: dark``. */
export const media = { prefersDark: false };
export interface Call {
  method: string;
  path: string;
  search: URLSearchParams;
  body: Json;
}
type Handler = (call: Call) => Json;

/** A fetch stub routed by ``"METHOD /path"``; records every call. */
export function mockApi(routes: Record<string, Handler>) {
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(String(input), init);
    const url = new URL(req.url, "http://127.0.0.1:8765");
    const text = req.method === "GET" ? "" : await req.text();
    const call: Call = {
      method: req.method,
      path: url.pathname,
      search: url.searchParams,
      body: text ? (JSON.parse(text) as Json) : undefined,
    };
    calls.push(call);
    const handler = routes[`${req.method} ${url.pathname}`];
    if (handler === undefined) {
      return Response.json({ error: { code: "not_found", message: "no mock" } }, { status: 404 });
    }
    const result = handler(call);
    const wrapped =
      result !== null && typeof result === "object" && "body" in result
        ? (result as { status?: number; body: Json })
        : { body: result };
    return Response.json(wrapped.body, { status: wrapped.status ?? 200 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

export function err(status: number, code: string, message = code) {
  return { status, body: { error: { code, message } } };
}

const seen = { location: "" };
function LocationProbe() {
  const loc = useLocation();
  useEffect(() => {
    seen.location = loc.pathname + loc.search;
  }, [loc]);
  return null;
}
export const currentLocation = (): string => seen.location;

export function renderApp(route = "/") {
  auth.reset();
  return render(
    <Providers client={newQueryClient()} env="test">
      <MemoryRouter initialEntries={[route]}>
        <App />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </Providers>,
  );
}

/** EventSource stand-in: tests push messages with ``emit``. */
export class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readonly url: string;
  readyState = 1;
  onerror: ((ev: Event) => void) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent<string>) => void)[]>();

  constructor(url: string | URL) {
    this.url = String(url);
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: (ev: MessageEvent<string>) => void): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), fn]);
  }
  close(): void {
    this.readyState = 2;
  }
  emit(type: string, data: unknown, id?: number): void {
    const ev = new MessageEvent<string>(type, {
      data: JSON.stringify(data),
      lastEventId: id === undefined ? "" : String(id),
    });
    for (const fn of this.listeners.get(type) ?? []) fn(ev);
  }
  static last(url: string): FakeEventSource {
    const found = [...FakeEventSource.instances].reverse().find((s) => s.url === url);
    if (found === undefined) throw new Error(`no EventSource for ${url}`);
    return found;
  }
}
