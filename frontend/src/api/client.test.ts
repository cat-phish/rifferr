import { expect, test } from "vitest";
import { RequestOrder } from "./client";

test("READ_ORDER_7 each page gets a fresh client id and an increasing seq", () => {
  const a = new RequestOrder("grid");
  const b = new RequestOrder("grid");
  expect(a.client).toMatch(/^grid-/);
  expect(a.client).not.toBe(b.client);
  expect([a.next(), a.next(), a.next()]).toEqual([0, 1, 2]);
  expect(b.next()).toBe(0);
});
