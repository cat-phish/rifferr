"""F-REVIEW-QUEUE in the worker: re-propose, decision notes, job results on the run's
stream, and current values (REVIEW-QUEUE-3, -5, -9, -10)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.selection import Selection
from rifferr.engine.changesets import CurrentTarget, current_values, repropose_targets
from tests.worker.test_review import RUN, Harness, serving
from tests.worker.test_review import _suspended as suspended  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    async with serving(tmp_path) as harness:
        yield harness


def _edit(h: Harness, **fields: object) -> None:
    """Someone changes the album while its ChangeSet waits for review."""
    album = h.backend.query(Selection())[0]
    h.backend.apply(
        ChangeSet(
            id=f"manual-{len(fields)}",
            profile="main",
            source=ChangeSource(kind="manual", name="test"),
            created_at=datetime.now(UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id=album.id),
                    changes=[
                        FieldChange(field=k, old=album.fields.get(k), new=v)  # type: ignore[arg-type]
                        for k, v in fields.items()
                    ],
                )
            ],
        )
    )


async def _conflicted(h: Harness, accepted: list[int] | None = None) -> str:
    (cs_id,) = await suspended(h, "A")
    _edit(h, year=1999)
    h.decide(cs_id, "accept", accepted)
    await h.until(lambda: (s := h.changesets.get(cs_id)) is not None and s.state == "conflicted")
    return cs_id


async def test_REVIEW_QUEUE_5_repropose_replaces_the_changeset_in_the_runs_holds(
    h: Harness,
) -> None:
    old_id = await _conflicted(h)
    job_id = await h.supervisor.repropose(old_id, by="ui")
    await h.until(lambda: (j := h.db.job(job_id)) is not None and j.state == "succeeded")
    old = h.changesets.get(old_id)
    assert old is not None
    (new_id,) = h.pending()
    assert (old.state, old.resolved_by) == ("rejected", f"re-proposed as {new_id}")
    new = h.changesets.get(new_id)
    assert new is not None
    assert new.changeset.step_id == old.changeset.step_id
    assert new.changeset.run_id == RUN
    assert [(c.field, c.old, c.new) for _, c in new.changeset.field_changes()] == [
        ("label", None, "L"),
        ("year", 1999, 2002),
    ]
    run = h.db.job_for_run(RUN)
    assert run is not None
    assert run.state == "waiting_review"
    assert run.payload["holds"] == [new_id]
    proposed = [e for e in h.db.events_since(RUN, 0) if e.type == "changeset.proposed"]
    assert proposed[-1].data["replaces"] == old_id
    finished = [e for e in h.db.events_since(RUN, 0) if e.type == "job.finished"]
    assert finished[-1].data["result"] == {"changeset_id": new_id}
    # The run now waits for the new ChangeSet, and accepting it resumes the run.
    h.decide(new_id, "accept")
    await h.until(lambda: h.state() == "succeeded")
    assert h.backend.query(Selection())[0].fields["year"] == 2002


async def test_REVIEW_QUEUE_5_repropose_keeps_only_the_accepted_field_changes(
    h: Harness,
) -> None:
    old_id = await _conflicted(h, accepted=[1])
    job_id = await h.supervisor.repropose(old_id)
    await h.until(lambda: (j := h.db.job(job_id)) is not None and j.state == "succeeded")
    (new_id,) = h.pending()
    new = h.changesets.get(new_id)
    assert new is not None
    assert [c.field for _, c in new.changeset.field_changes()] == ["year"]


async def test_REVIEW_QUEUE_5_nothing_left_to_change_rejects_and_resumes(h: Harness) -> None:
    (cs_id,) = await suspended(h, "A")
    _edit(h, year=1999)
    h.decide(cs_id, "accept")
    await h.until(lambda: (s := h.changesets.get(cs_id)) is not None and s.state == "conflicted")
    _edit(h, year=2002, label="L")  # someone made the same changes meanwhile
    job_id = await h.supervisor.repropose(cs_id, by="ui")
    await h.until(lambda: h.state() == "succeeded")
    job = h.db.job(job_id)
    assert job is not None
    assert (job.state, job.result) == ("succeeded", {"changeset_id": None})
    old = h.changesets.get(cs_id)
    assert old is not None
    assert (old.state, old.resolved_by) == ("rejected", "ui")
    assert h.pending() == []


async def test_REVIEW_QUEUE_5_only_conflicted_changesets_can_be_reproposed(h: Harness) -> None:
    (cs_id,) = await suspended(h, "A")
    with pytest.raises(ValueError, match="not conflicted"):
        await h.supervisor.repropose(cs_id)
    with pytest.raises(KeyError):
        await h.supervisor.repropose("nope")


async def test_REVIEW_QUEUE_9_decision_records_who_decided_and_the_note(h: Harness) -> None:
    (cs_id,) = await suspended(h, "A")
    await h.supervisor.decide(cs_id, accept=False, by="ui", note="wrong label")
    await h.until(lambda: h.state() == "succeeded")
    stored = h.changesets.get(cs_id)
    assert stored is not None
    assert (stored.state, stored.resolved_by) == ("rejected", "ui")
    assert h.changesets.note(cs_id) == "wrong label"


async def test_REVIEW_QUEUE_10_job_results_go_on_the_runs_stream(h: Harness) -> None:
    (cs_id,) = await suspended(h, "A")
    job_id = h.decide(cs_id, "accept")
    await h.until(lambda: h.state() == "succeeded")
    (finished,) = [e for e in h.db.events_since(RUN, 0) if e.type == "job.finished"]
    assert finished.data["job_id"] == job_id
    assert (finished.data["kind"], finished.data["state"]) == ("review_decision", "succeeded")
    undo = await h.supervisor.undo(run_id=RUN, by="ui")
    await h.until(lambda: "job.finished" in h.types()[-1:] and h.types().count("job.finished") == 2)
    last = h.db.events_since(RUN, 0)[-1]
    assert last.data["job_id"] == undo
    result = last.data["result"]
    assert isinstance(result, dict)
    assert len(result["undone"]) == 1  # type: ignore[arg-type]
    seqs = [e.seq for e in h.db.events_since(RUN, 0)]
    assert seqs == list(range(1, len(seqs) + 1))


# -- current values (REVIEW-QUEUE-3) ------------------------------------------------------


def _changeset(*targets: TargetChange) -> ChangeSet:
    return ChangeSet(
        id="cs",
        profile="main",
        run_id="r",
        source=ChangeSource(kind="step", name="test"),
        created_at=datetime.now(UTC),
        targets=list(targets),
    )


def test_REVIEW_QUEUE_3_current_values_of_the_changed_fields_per_target() -> None:
    fb = FakeBackend()
    fb.add_album(
        {
            "album": "Live",
            "albumartist": "Band",
            "year": 2001,
            "label": "X",
            "items": [{"title": "One", "track": 1, "genre": "rock"}],
        }
    )
    album = fb.query(Selection())[0]
    item = album.items[0]
    cs = _changeset(
        TargetChange(
            target=TargetRef(scope="album", id=album.id),
            changes=[
                FieldChange(field="album", old="Live", new="Live (EP)"),
                FieldChange(field="albumdisambig", old=None, new="EP"),
            ],
        ),
        TargetChange(
            target=TargetRef(scope="item", id=item.id),
            changes=[FieldChange(field="genre", old="rock", new="pop")],
        ),
        TargetChange(
            target=TargetRef(scope="album", id="gone"),
            changes=[FieldChange(field="year", old=1, new=2)],
        ),
    )
    before = fb.query(Selection())
    current = current_values(fb.read_only_view(), cs)
    assert fb.query(Selection()) == before  # wrote nothing
    assert [(c.label, c.missing, c.fields) for c in current] == [
        ("Band - Live", False, {"album": "Live"}),
        ("1. One", False, {"genre": "rock"}),
        ("album gone", True, {}),
    ]


def test_REVIEW_QUEUE_5_repropose_targets_take_old_from_current_and_drop_no_ops() -> None:
    a = TargetRef(scope="album", id="a")
    cs = _changeset(
        TargetChange(
            target=a,
            changes=[
                FieldChange(field="album", old="Live", new="Live (EP)"),
                FieldChange(field="year", old=2001, new=2002),
                FieldChange(field="comments", op="delete", old="x"),
            ],
        )
    )
    now = [CurrentTarget(target=a, label="A", fields={"album": "Live!", "year": 2002})]
    (target,) = repropose_targets(cs, now)
    assert [(c.field, c.old, c.new) for c in target.changes] == [("album", "Live!", "Live (EP)")]
    assert repropose_targets(cs, [CurrentTarget(target=a, label="A", missing=True)]) == []
    (only,) = repropose_targets(cs, now, accepted=[0])
    assert [c.field for c in only.changes] == ["album"]
