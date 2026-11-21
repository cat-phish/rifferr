import { describe, expect, it } from "vitest";
import type { LaneOut, QueueItem } from "../api/types";
import { groupByLane, laneCounts, laneStatus } from "./review";

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

describe("groupByLane", () => {
  it("UI_REFRESH_13_one_block_per_lane_in_lane_order_items_in_step_order", () => {
    const items = [
      item("i2", "covers", "2026-01-01T00:00:02Z"),
      item("i1", "import", "2026-01-01T00:00:03Z"),
      item("i3", "import", "2026-01-01T00:00:01Z"),
    ];
    const lanes = [lane("b", "waiting", ["i3"]), lane("a", "waiting", ["i2", "i1"])];
    const blocks = groupByLane(items, lanes, STEPS);
    expect(blocks.map((b) => b.key)).toEqual(["b", "a"]);
    expect(blocks[1]?.items.map((i) => i.id)).toEqual(["i1", "i2"]);
  });

  it("UI_REFRESH_13_unheld_and_shared_items_come_last", () => {
    const items = [item("g", "gate", "t1"), item("x", "move", "t2"), item("i", "import", "t3")];
    const lanes = [lane("a", "waiting", ["g", "i"]), lane("b", "waiting", ["g"])];
    const blocks = groupByLane(items, lanes, STEPS);
    expect(blocks.map((b) => [b.key, b.items.map((i) => i.id)])).toEqual([
      ["a", ["i"]],
      ["b", []],
      ["*several", ["g"]],
      ["*other", ["x"]],
    ]);
  });

  it("UI_REFRESH_15_finished_lanes_get_no_block_but_working_ones_do", () => {
    const lanes = [
      lane("a", "done", []),
      lane("b", "active", [], "covers"),
      lane("c", "failed", []),
    ];
    const blocks = groupByLane([], lanes, STEPS);
    expect(blocks.map((b) => b.key)).toEqual(["b"]);
    expect(laneCounts(lanes)).toBe("1 active · 1 done · 1 failed");
  });
});

describe("laneStatus", () => {
  it("UI_REFRESH_14_status_line", () => {
    const waiting = {
      key: "a",
      label: "A",
      lane: lane("a", "waiting", ["i"]),
      items: [item("i", "import", "t")],
    };
    expect(laneStatus(waiting)).toEqual({ text: "waiting on you", token: "warning" });
    const running = { key: "a", label: "A", lane: lane("a", "active", [], "covers"), items: [] };
    expect(laneStatus(running)).toEqual({ text: "running covers", token: null });
    const done = { key: "a", label: "A", lane: lane("a", "done", []), items: [] };
    expect(laneStatus(done)).toEqual({ text: "done", token: "success" });
  });
});
