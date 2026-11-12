import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { FakeEventSource, media } from "./test-utils";

// jsdom lacks these; Mantine needs them.
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: query.includes("dark") ? media.prefersDark : false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
});
class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
globalThis.ResizeObserver = NoopResizeObserver;
window.HTMLElement.prototype.scrollIntoView = () => {};
// jsdom has no layout; give elements a size so the virtualized log renders rows.
Object.defineProperty(HTMLElement.prototype, "clientHeight", {
  configurable: true,
  get: () => 400,
});
Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 });
globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;

afterEach(() => {
  cleanup();
  media.prefersDark = false;
  vi.unstubAllGlobals();
  FakeEventSource.instances.length = 0;
});
