import { expect, test } from "vitest";
import { ApiError, SUPERSEDED_RETRIES, retrySuperseded } from "./client";

const error = (status: number, code: string) =>
  new ApiError(status, { error: { code, message: code } });

test("retrySuperseded retries a superseded answer a few times (B-053)", () => {
  const superseded = error(409, "superseded");
  expect(retrySuperseded(0, superseded)).toBe(true);
  expect(retrySuperseded(SUPERSEDED_RETRIES - 1, superseded)).toBe(true);
  expect(retrySuperseded(SUPERSEDED_RETRIES, superseded)).toBe(false);
});

test("retrySuperseded leaves other errors alone", () => {
  expect(retrySuperseded(0, error(400, "bad_query"))).toBe(false);
  expect(retrySuperseded(0, error(504, "query_timeout"))).toBe(false);
  expect(retrySuperseded(0, new Error("network"))).toBe(false);
});
