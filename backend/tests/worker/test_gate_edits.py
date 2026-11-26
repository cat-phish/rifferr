"""Edits held at an `edit: true` review gate (S-REVIEW REVIEW-13 .. -15)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from rifferr.core.changeset import FieldChange, TargetRef
from rifferr.core.review import HeldEdit, ReviewGate
from rifferr.core.selection import Selection
from tests.worker.conftest import wf
from tests.worker.test_gates import AFTER, TAG, Harness, gate_with, serving

EDIT_GATE = wf(TAG, gate_with(edit="true", show="[label]"), AFTER)


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


def album_id(h: Harness, name: str) -> str:
    return next(a.id for a in h.backend.query(Selection()) if a.fields["album"] == name)


def field(h: Harness, name: str, key: str) -> object:
    return next(a for a in h.backend.query(Selection()) if a.fields["album"] == name).fields.get(
        key
    )


def held(h: Harness, name: str, key: str, old: object, new: object) -> HeldEdit:
    aid = album_id(h, name)
    return HeldEdit(
        target=TargetRef(scope="album", id=aid),
        album=aid,
        changes=[FieldChange(field=key, old=old, new=new)],  # type: ignore[arg-type]
    )


async def gate_job_done(h: Harness, before: int) -> str:
    def done() -> bool:
        jobs = [j for j in h.db.jobs("main") if j.kind == "gate_apply"]
        return len(jobs) > before and jobs[-1].state in ("succeeded", "failed")

    await h.until(done)
    return [j for j in h.db.jobs("main") if j.kind == "gate_apply"][-1].state


async def test_REVIEW_14_edits_are_held_not_written(h: Harness) -> None:
    gate = await h.waiting(EDIT_GATE)
    assert gate.edit
    h.gates.set_edits(gate.id, [held(h, "A", "label", "L", "Apple")])
    stored = h.gates.get(gate.id)
    assert stored is not None
    assert stored.edits[0].changes[0].new == "Apple"
    assert field(h, "A", "label") == "L"  # nothing written while pending


async def test_REVIEW_15_continue_writes_the_held_edits_then_goes_on(h: Harness) -> None:
    gate = await h.waiting(EDIT_GATE)
    h.gates.set_edits(gate.id, [held(h, "A", "label", "L", "Apple")])
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    assert await gate_job_done(h, 0) == "succeeded"
    await h.until(lambda: h.state() == "succeeded")
    assert field(h, "A", "label") == "Apple"
    assert "after: continue" in h.logs()
    decided: ReviewGate | None = h.gates.get(gate.id)
    assert decided is not None
    assert decided.state == "continued"
    applied = [
        s for s in h.changesets.for_run(gate.run_id, "applied") if s.changeset.step_id == "check"
    ]
    assert applied  # journaled with the run, so it can be undone with it


async def test_REVIEW_15_a_conflicting_edit_writes_nothing_and_keeps_waiting(h: Harness) -> None:
    gate = await h.waiting(EDIT_GATE)
    # The reviewer saw "X", but the album says "L": the edit conflicts.
    h.gates.set_edits(
        gate.id,
        [held(h, "A", "label", "X", "Apple"), held(h, "A", "album", "A", "A2")],
    )
    await h.supervisor.decide_gate(gate.id, "continue", by="ui")
    assert await gate_job_done(h, 0) == "failed"
    assert field(h, "A", "label") == "L"
    assert field(h, "A", "album") == "A"  # the other edit wasn't written either
    stored = h.gates.get(gate.id)
    assert stored is not None
    assert stored.state == "pending"
    assert len(stored.edits) == 2  # still held
    assert h.state() == "waiting_review"
    job = [j for j in h.db.jobs("main") if j.kind == "gate_apply"][-1]
    assert "label" in (job.error or "")


async def test_REVIEW_15_reject_discards_the_held_edits(h: Harness) -> None:
    gate = await h.waiting(EDIT_GATE)
    h.gates.set_edits(gate.id, [held(h, "A", "label", "L", "Apple")])
    await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    await h.until(lambda: h.state() == "cancelled")
    assert field(h, "A", "label") == "L"
    assert not [j for j in h.db.jobs("main") if j.kind == "gate_apply"]
