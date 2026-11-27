import { expect, test } from "vitest";
import type { components } from "../api/schema";
import { mockApi } from "../test-utils";
import { decideAll } from "./bulk";

type S = components["schemas"];

function item(id: string, warnings: string[][]): S["QueueItem"] {
  return {
    busy: false,
    kind: "changeset",
    id,
    profile: "main",
    run_id: "R1",
    workflow: "w",
    step_id: "t",
    created_at: "2026-11-26T12:00:00Z",
    state: "proposed",
    gate: null,
    changeset: {
      id,
      profile: "main",
      run_id: "R1",
      step_id: "t",
      source: { kind: "ai", name: "ai.tag" },
      rationale: null,
      created_at: "2026-11-26T12:00:00Z",
      targets: [
        {
          target: { scope: "album", id: "a1" },
          changes: warnings.map((w, i) => ({
            field: `f${String(i)}`,
            op: "set" as const,
            old: null,
            new: "x",
            warnings: w,
          })),
        },
      ],
    },
  };
}

test("AI_TAG_10 bulk accept leaves out changes with warnings", async () => {
  const { calls } = mockApi({
    "POST /api/changesets/c1/decision": () => ({ status: 202, body: { job_id: "j1" } }),
    "POST /api/changesets/c2/decision": () => ({ status: 202, body: { job_id: "j2" } }),
    "POST /api/changesets/c3/decision": () => ({ status: 202, body: { job_id: "j3" } }),
  });
  await decideAll(
    [item("c1", [[], ["not_in_vocabulary"]]), item("c2", [[]]), item("c3", [["x"]])],
    "accept",
  );
  expect(calls.map((c) => [c.path, c.body])).toEqual([
    ["/api/changesets/c1/decision", { decision: "accept", accepted: [0] }],
    ["/api/changesets/c2/decision", { decision: "accept" }],
  ]); // c3: every change has a warning, so it waits for a look
});
