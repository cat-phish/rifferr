import { describe, expect, it } from "vitest";
import type { LaneOut, QueueItem } from "../api/types";
import { type AlbumBlock, groupByLane, laneCounts, laneStatus } from "./review";

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
  it("UI_REFRESH_13_waiting_albums_first_come_first_items_in_step_order", () => {
    const items = [
      item("i2", "covers", "2026-01-01T00:00:02Z"),
      item("i1", "import", "2026-01-01T00:00:03Z"),
      item("i3", "import", "2026-01-01T00:00:01Z"),
    ];
    const lanes = [lane("a", "waiting", ["i2", "i1"]), lane("b", "waiting", ["i3"])];
    const { waiting } = groupByLane(items, lanes, STEPS);
    // b's item is older than any of a's, so b comes first, whatever the lane order.
    expect(waiting.map((b) => b.key)).toEqual(["b", "a"]);
    expect(waiting[1]?.items.map((i) => i.id)).toEqual(["i1", "i2"]);
  });

  it("UI_REFRESH_13_an_album_whose_next_item_arrives_joins_the_bottom", () => {
    const lanes = [lane("a", "waiting", ["move-a"]), lane("b", "waiting", ["imp-b"])];
    const items = [item("imp-b", "import", "t1"), item("move-a", "move", "t5")];
    expect(groupByLane(items, lanes, STEPS).waiting.map((b) => b.key)).toEqual(["b", "a"]);
  });

  it("UI_REFRESH_13_shared_and_unheld_items_get_blocks_of_their_own", () => {
    const items = [item("g", "gate", "t1"), item("x", "move", "t2"), item("i", "import", "t3")];
    const lanes = [lane("a", "waiting", ["g", "i"]), lane("b", "waiting", ["g"])];
    const { waiting, working } = groupByLane(items, lanes, STEPS);
    expect(waiting.map((b) => [b.key, b.items.map((i) => i.id)])).toEqual([
      ["*several", ["g"]],
      ["*other", ["x"]],
      ["a", ["i"]],
    ]);
    expect(working.map((b) => b.key)).toEqual(["b"]);
  });

  it("UI_REFRESH_15_finished_lanes_get_no_block_working_ones_are_listed", () => {
    const lanes = [
      lane("a", "done", []),
      lane("b", "active", [], "covers"),
      lane("c", "failed", []),
    ];
    const { waiting, working } = groupByLane([], lanes, STEPS);
    expect(waiting).toEqual([]);
    expect(working.map((b) => b.key)).toEqual(["b"]);
    expect(laneCounts(lanes)).toBe("1 active · 1 done · 1 failed");
  });

  it("UI_REFRESH_20_decided_items_leave_the_waiting_list", () => {
    const lanes = [lane("a", "waiting", ["i1"]), lane("b", "waiting", ["i2"])];
    const items = [item("i1", "import", "t1"), item("i2", "import", "t2")];
    const { waiting, working } = groupByLane(items, lanes, STEPS, new Set(["i1"]));
    expect(waiting.map((b) => b.key)).toEqual(["b"]);
    expect(working.map((b) => [b.key, b.decided.map((i) => i.id)])).toEqual([["a", ["i1"]]]);
    expect(laneStatus(working[0] as AlbumBlock).text).toBe("working on your decision");
  });
});

describe("laneStatus", () => {
  const block = (l: LaneOut, items: QueueItem[] = []): AlbumBlock => ({
    key: l.key,
    label: l.label,
    lane: l,
    items,
    decided: [],
  });
  it("UI_REFRESH_14_status_line", () => {
    expect(laneStatus(block(lane("a", "waiting", ["i"]), [item("i", "import", "t")]))).toEqual({
      text: "waiting on you",
      token: "warning",
    });
    expect(laneStatus(block(lane("a", "active", [], "covers")))).toEqual({
      text: "running covers",
      token: null,
    });
    expect(laneStatus(block(lane("a", "done", [])))).toEqual({ text: "done", token: "success" });
  });
});
