import { describe, expect, it } from "vitest";
import type { LaneOut, QueueItem } from "../api/types";
import { groupByStep, laneOf } from "./review";

function item(id: string, step: string, at: string): QueueItem {
  return {
    id,
    kind: "import",
    run_id: "r1",
    profile: "main",
    workflow: "wf",
    step_id: step,
    created_at: at,
  };
}

function lane(
  key: string,
  state: LaneOut["state"],
  refs: string[],
  position: string | null = null,
): LaneOut {
  return {
    key,
    label: `Album ${key}`,
    state,
    position,
    holds: refs.map((ref) => ({ kind: "import" as const, ref })),
    steps: {},
  };
}

const STEPS = ["import", "covers", "gate", "move"];

const ORDERS = new Map([["wf", STEPS]]);
const ids = (secs: ReturnType<typeof groupByStep>) =>
  secs.map((sec) => [sec.label, sec.items.map((i) => i.id)]);

describe("groupByStep", () => {
  it("UI_REFRESH_13_sections_in_step_order_items_oldest_first", () => {
    const items = [
      item("m2", "move", "t5"),
      item("i2", "import", "t3"),
      item("m1", "move", "t2"),
      item("i1", "import", "t4"),
    ];
    // The newest import (t4) still goes below the older one; moves come after imports
    // because the workflow runs import first, whatever arrived first.
    expect(ids(groupByStep(items, ORDERS))).toEqual([
      ["import", ["i2", "i1"]],
      ["move", ["m1", "m2"]],
    ]);
  });

  it("UI_REFRESH_13_unknown_steps_last_and_workflow_prefix_with_several_workflows", () => {
    const other = { ...item("x", "tag", "t1"), workflow: "other" };
    const secs = groupByStep([item("i", "import", "t2"), item("z", "zzz", "t0"), other], ORDERS);
    expect(secs.map((sec) => sec.label)).toEqual(["other · tag", "wf · import", "wf · zzz"]);
  });

  it("UI_REFRESH_20_decided_items_leave_and_empty_sections_go", () => {
    const items = [item("i1", "import", "t1"), item("m1", "move", "t2"), item("m2", "move", "t3")];
    expect(ids(groupByStep(items, ORDERS, new Set(["i1", "m1"])))).toEqual([["move", ["m2"]]]);
  });
});

describe("laneOf", () => {
  it("REVIEW_QUEUE_11_album_label_from_the_one_lane_holding_the_item", () => {
    const lanes = [lane("a", "waiting", ["i1", "g"]), lane("b", "waiting", ["g"])];
    expect(laneOf(item("i1", "import", "t"), lanes)?.label).toBe("Album a");
    expect(laneOf(item("g", "gate", "t"), lanes)).toBeNull();
    expect(laneOf(item("x", "import", "t"), lanes)).toBeNull();
  });
});
