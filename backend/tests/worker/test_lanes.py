"""C-LANES in the worker and supervisor: resume jobs, gate rejection, cancel."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rifferr.appdb import AppLanes
from rifferr.core.selection import Selection
from rifferr.worker.gates import SendBackError, back_targets
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, TAG, Harness, gate_with, serving

# The gate looks at B only; A and C go on to `after`.
GATE_B = "{id: check, uses: review, when: 'album:B', with: {message: 'Check $album'}}"
AFTER = "{id: after, uses: test.per-album}"
REVIEW_C = "{id: comp, uses: test.propose, when: 'album:C', with: {set: {genre: Pop}}}"


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


def lane_states(h: Harness) -> dict[str, str]:
    return {lane.label: lane.state for lane in AppLanes(h.db).load(RUN).values()}


def labels(h: Harness) -> dict[str, object]:
    return {str(a.fields["album"]): a.fields.get("label") for a in h.backend.query(Selection())}


async def test_LANES_7_waiting_run_holds_no_worker_while_others_finished(h: Harness) -> None:
    await h.waiting(wf(TAG, GATE_B, AFTER), "A", "B", "C")
    assert lane_states(h) == {"A": "done", "B": "waiting", "C": "done"}
    assert [line for line in h.logs() if line.startswith("processed")] == [
        "processed A",
        "processed C",
    ]


async def test_LANES_8_released_lane_queues_one_resume_job(h: Harness) -> None:
    source = wf(TAG, GATE_B, REVIEW_C, AFTER)
    gate = await h.waiting(source, "A", "B", "C")
    assert lane_states(h) == {"A": "done", "B": "waiting", "C": "waiting"}
    (pending,) = h.changesets.for_run(RUN, "proposed")
    # Rejecting C's ChangeSet resolves its hold; the run is queued and C finishes.
    job = await h.supervisor.decide(pending.changeset.id, accept=False, by="ui")
    await h.until(lambda: (j := h.db.job(job)) is not None and j.state == "succeeded")
    await h.until(lambda: lane_states(h)["C"] == "done")
    await h.until(lambda: h.state() == "waiting_review")  # B still waits
    assert [j.kind for j in h.db.jobs("main")].count("run") == 1  # the same run job
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert lane_states(h) == {"A": "done", "B": "done", "C": "done"}


async def test_LANES_11_gate_reject_ends_only_its_albums(h: Harness) -> None:
    source = wf(TAG, gate_with(on_reject="undo", message="'Check $album'"), AFTER)
    source = source.replace(
        "{id: check, uses: review,", "{id: check, uses: review, when: 'album:B',"
    )
    gate = await h.waiting(source, "A", "B", "C")
    assert labels(h) == {"A": "L", "B": "L", "C": "L"}
    await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert lane_states(h) == {"A": "done", "B": "rejected", "C": "done"}
    assert labels(h) == {"A": "L", "B": None, "C": "L"}  # only B's changes undone
    assert h.events("run.finished")[-1].data == {"status": "succeeded"}


async def test_LANES_11_every_lane_rejected_cancels_the_run(h: Harness) -> None:
    gate = await h.waiting(wf(TAG, gate_with(on_reject="stop", message="m"), AFTER), "A", "B")
    await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    assert h.state() == "cancelled"
    assert lane_states(h) == {"A": "rejected", "B": "rejected"}
    assert h.events("run.finished")[-1].data == {
        "status": "cancelled",
        "reason": "rejected_at_review",
    }


async def test_LANES_11_reject_with_another_lane_still_waiting_keeps_the_run_waiting(
    h: Harness,
) -> None:
    gate = await h.waiting(wf(TAG, GATE_B, REVIEW_C, AFTER), "A", "B", "C")
    await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    assert h.state() == "waiting_review"
    assert lane_states(h) == {"A": "done", "B": "rejected", "C": "waiting"}


async def test_LANES_13_cancel_while_waiting_cancels_open_lanes(h: Harness) -> None:
    await h.waiting(wf(TAG, GATE_B, AFTER), "A", "B")
    await h.supervisor.cancel(RUN)
    assert h.state() == "cancelled"
    assert lane_states(h) == {"A": "done", "B": "cancelled"}
    assert [e.data["state"] for e in h.events("lane.finished")][-1] == "cancelled"


# -- LANES-17: back to an earlier review gate ------------------------------------------

CHECK_ALL = "{id: check, uses: review, with: {message: 'Check $album'}}"
PROPOSE_ALL = "{id: comp, uses: test.propose, with: {set: {genre: Pop}}}"


async def _held_at_comp(h: Harness) -> dict[str, str]:
    """A and B continued at `check`, both held by `comp`'s ChangeSet: album -> ChangeSet."""
    gate = await h.waiting(wf(TAG, CHECK_ALL, PROPOSE_ALL, AFTER), "A", "B")
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    await h.until(lambda: len(h.changesets.for_run(RUN, "proposed")) == 2)
    await h.until(lambda: h.state() == "waiting_review")
    lanes = AppLanes(h.db).load(RUN).values()
    return {lane.label: lane.holds[0].ref for lane in lanes}


async def test_LANES_17_back_reopens_the_gate_for_that_album_only(h: Harness) -> None:
    held = await _held_at_comp(h)
    key = {lane.label: lane.key for lane in AppLanes(h.db).load(RUN).values()}
    lane = AppLanes(h.db).load(RUN)[key["A"]]
    order = [("tag", "test.tag"), ("check", "review"), ("comp", "test.propose"), ("after", "x")]
    assert back_targets(order, lane, "comp") == ["check"]
    assert back_targets(order, lane, "check") == []
    await h.supervisor.send_back(RUN, key["A"], "check")
    stored = h.changesets.get(held["A"])
    assert stored is not None
    assert stored.state == "rejected"
    await h.until(lambda: any(g.state == "pending" for g in h.gates.for_run(RUN)))
    await h.until(lambda: h.state() == "waiting_review")
    (again,) = [g for g in h.gates.for_run(RUN) if g.state == "pending"]
    assert [a.target.id for a in again.albums] == [key["A"]]
    assert lane_states(h) == {"A": "waiting", "B": "waiting"}
    other = h.changesets.get(held["B"])
    assert other is not None
    assert other.state == "proposed"
    assert "A: sent back to check" in h.logs()
    # Continue: A gets a new ChangeSet from `comp`.
    await h.supervisor.decide_gate(again.id, "continue", by="ui")
    await h.until(lambda: len(h.changesets.for_run(RUN, "proposed")) == 2)
    for stored in h.changesets.for_run(RUN, "proposed"):
        await h.supervisor.decide(stored.changeset.id, accept=True, by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert lane_states(h) == {"A": "done", "B": "done"}


async def test_LANES_17_only_a_lane_waiting_on_changesets_goes_back(h: Harness) -> None:
    gate = await h.waiting(wf(TAG, CHECK_ALL, PROPOSE_ALL, AFTER), "A")
    (lane,) = AppLanes(h.db).load(RUN).values()
    with pytest.raises(SendBackError, match="waits on a gate"):
        await h.supervisor.send_back(RUN, lane.key, "check")
    assert h.gates.get(gate.id) is not None
    with pytest.raises(KeyError):
        await h.supervisor.send_back(RUN, "nope", "check")
