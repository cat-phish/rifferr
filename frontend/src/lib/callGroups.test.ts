import { describe, expect, it } from "vitest";
import type { StepState } from "../api/types";
import { stepRows } from "./callGroups";

const step = (id: string, call_path: string[] = [], workflow?: string): StepState => ({
  id,
  uses: "x",
  status: "pending",
  outputs: {},
  passes: 0,
  call_path,
  workflow: workflow ?? null,
  workflow_name: workflow ? `Name of ${workflow}` : null,
});

describe("stepRows (SUBWORKFLOW-10)", () => {
  it("groups called steps under one header per call", () => {
    const rows = stepRows([
      step("import"),
      step("tagging/fix-case", ["tagging"], "tagging.yaml"),
      step("tagging/genres", ["tagging"], "tagging.yaml"),
      step("post/tagging/a", ["post", "tagging"], "tagging.yaml"),
      step("tell"),
    ]);
    expect(rows.map((r) => (r.kind === "call" ? `call:${r.id}:${r.label}` : r.step.id))).toEqual([
      "import",
      "call:tagging:Name of tagging.yaml",
      "tagging/fix-case",
      "tagging/genres",
      "call:post:post",
      "call:post/tagging:Name of tagging.yaml",
      "post/tagging/a",
      "tell",
    ]);
    expect(rows.map((r) => r.depth)).toEqual([0, 0, 1, 1, 0, 1, 2, 0]);
  });
});
