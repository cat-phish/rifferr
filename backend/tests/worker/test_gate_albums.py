"""Per-album decisions on a multi-album review gate (F-DASHBOARD DASHBOARD-11)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rifferr.appdb import GateAlbumsError, GateResolvedError
from rifferr.core.review import ReviewGate
from tests.worker.conftest import wf
from tests.worker.test_gates import TAG, Harness, gate_with, serving
from tests.worker.test_lanes import AFTER, labels, lane_states


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


def ids(h: Harness, gate: ReviewGate) -> dict[str, str]:
    return {a.label.split(" - ")[-1]: a.target.id for a in gate.albums}


async def test_DASHBOARD_11_continue_some_albums_the_others_keep_waiting(h: Harness) -> None:
    gate = await h.waiting(wf(TAG, gate_with(message="m"), AFTER), "A", "B", "C")
    album = ids(h, gate)
    await h.supervisor.decide_gate(gate.id, "continue", by="ui", albums=[album["A"]])
    await h.until(lambda: lane_states(h)["A"] == "done")
    await h.until(lambda: h.state() == "waiting_review")
    assert lane_states(h) == {"A": "done", "B": "waiting", "C": "waiting"}
    stored = h.gates.get(gate.id)
    assert stored is not None
    assert stored.state == "pending"
    assert stored.album_decisions == {album["A"]: "continued"}
    assert stored.undecided() == [album["B"], album["C"]]
    assert [e.data["albums"] for e in h.events("review.decided")] == [[album["A"]]]
    # Deciding the rest resolves the gate.
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert lane_states(h) == {"A": "done", "B": "done", "C": "done"}
    resolved = h.gates.get(gate.id)
    assert resolved is not None
    assert resolved.state == "continued"
    assert h.events("review.resolved")[-1].data["albums"] == [album["B"], album["C"]]


async def test_DASHBOARD_11_reject_some_albums_with_undo(h: Harness) -> None:
    source = wf(TAG, gate_with(on_reject="undo", message="m"), AFTER)
    gate = await h.waiting(source, "A", "B")
    album = ids(h, gate)
    await h.supervisor.decide_gate(gate.id, "reject", by="ui", albums=[album["B"]])
    await h.until(lambda: lane_states(h)["B"] == "rejected")
    assert labels(h) == {"A": "L", "B": None}  # only B's changes undone
    assert h.state() == "waiting_review"
    await h.supervisor.decide_gate(gate.id, "continue", by="ui", albums=[album["A"]])
    await h.until(lambda: h.state() == "succeeded")
    assert lane_states(h) == {"A": "done", "B": "rejected"}
    resolved = h.gates.get(gate.id)
    assert resolved is not None
    assert resolved.state == "continued"
    assert resolved.album_decisions == {album["B"]: "rejected", album["A"]: "continued"}


async def test_DASHBOARD_11_albums_must_be_undecided_albums_of_the_gate(h: Harness) -> None:
    gate = await h.waiting(wf(TAG, gate_with(message="m"), AFTER), "A", "B")
    album = ids(h, gate)
    with pytest.raises(GateAlbumsError):
        await h.supervisor.decide_gate(gate.id, "continue", albums=["album:nosuch"])
    await h.supervisor.decide_gate(gate.id, "reject", albums=[album["A"]])
    with pytest.raises(GateAlbumsError):
        await h.supervisor.decide_gate(gate.id, "continue", albums=[album["A"]])
    await h.supervisor.decide_gate(gate.id, "reject", albums=[album["B"]])
    assert h.state() == "cancelled"
    with pytest.raises(GateResolvedError):
        await h.supervisor.decide_gate(gate.id, "continue")
