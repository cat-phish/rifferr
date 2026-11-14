"""C-LANES LANES-15: the run's lanes in the API."""

from __future__ import annotations

from rifferr.appdb import AppLanes
from rifferr.engine import Hold, Lane
from tests.api.conftest import ECHO, Api


async def test_LANES_15_run_detail_lists_lanes_and_summary_counts_them(api: Api) -> None:
    api.add_workflow("echo", ECHO)
    run_id = await api.start("echo")
    await api.wait_finished(run_id)
    AppLanes(api.state.db).save(
        run_id,
        [
            Lane("album-1", "Artist - A", "done", None, steps={"a": "succeeded"}),
            Lane(
                "album-2",
                "Artist - B",
                "waiting",
                "b",
                [Hold("gate", "G1")],
                {"a": "succeeded"},
            ),
        ],
    )
    detail = (await api.client.get(f"/api/runs/{run_id}")).json()
    assert detail["lanes"] == [
        {
            "key": "album-1",
            "label": "Artist - A",
            "state": "done",
            "position": None,
            "holds": [],
            "steps": {"a": "succeeded"},
        },
        {
            "key": "album-2",
            "label": "Artist - B",
            "state": "waiting",
            "position": "b",
            "holds": [{"kind": "gate", "ref": "G1"}],
            "steps": {"a": "succeeded"},
        },
    ]
    assert detail["lane_counts"] == {"done": 1, "waiting": 1}
    assert [s["passes"] for s in detail["steps"]] == [1, 1]
    (listed,) = (await api.client.get("/api/runs")).json()
    assert listed["lane_counts"] == {"done": 1, "waiting": 1}


def test_LANES_2_lane_events_go_on_the_all_runs_stream() -> None:
    from datetime import UTC, datetime

    from rifferr.api.state import on_all_stream
    from rifferr.engine import Event

    for type in ("lane.started", "lane.waiting", "lane.resumed", "lane.finished"):
        event = Event(seq=1, ts=datetime.now(UTC), run_id="r", type=type, step_id=None, data={})
        assert on_all_stream(event)
