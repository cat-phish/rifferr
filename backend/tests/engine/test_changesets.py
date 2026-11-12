"""C-CHANGESET in the engine: propose, apply policy, events, suspension (RUN-14)."""

from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER, BackendCapabilities
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import ApplyResult, ChangeSet
from rifferr.core.selection import Selection
from rifferr.engine import Run, RunState, RunValidationError
from tests.engine.conftest import Harness


def wf(*steps: str) -> str:
    return "name: cs\nsteps:\n" + "".join(f"  - {s}\n" for s in steps)


def album(h: Harness, run: Run, name: str, **fields: Any) -> None:
    items = fields.pop("items", [{"title": f"{name} 1", "track": 1}])
    h.backend.add_album({"album": name, RUN_MARKER: run.id, **fields, "items": items})


def library(h: Harness) -> dict[str, dict[str, Any]]:
    return {str(a.fields["album"]): dict(a.fields) for a in h.backend.query(Selection())}


def types(h: Harness, run: Run) -> list[str]:
    return [t for t, _ in h.types(run)]


def data(h: Harness, run: Run, type: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.events(run) if e.type == type]


# -- CHANGESET-3: ctx.propose ---------------------------------------------------------


async def test_CHANGESET_3_short_form_targets_the_current_album_and_fills_old(
    h: Harness,
) -> None:
    run = h.run(wf("{id: p, uses: test.propose, apply: auto, with: {set: {year: 2002}}}"))
    album(h, run, "A", year=2001)
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    (stored,) = h.engine.changesets.for_run(run.id)
    cs = stored.changeset
    assert (cs.run_id, cs.step_id, cs.profile) == (run.id, "p", "main")
    assert (cs.source.kind, cs.source.name) == ("step", "test.propose")
    (target,) = cs.targets
    assert target.target.scope == "album"
    assert (target.changes[0].old, target.changes[0].new) == (2001, 2002)
    assert library(h)["A"]["year"] == 2002


async def test_CHANGESET_3_explicit_old_is_kept_and_can_conflict(h: Harness) -> None:
    run = h.run(
        wf("{id: p, uses: test.propose, apply: auto, with: {set: {year: 2002}, old: {year: 1999}}}")
    )
    album(h, run, "A", year=2001)
    await h.engine.execute(run)
    (stored,) = h.engine.changesets.for_run(run.id)
    assert stored.state == "conflicted"
    assert library(h)["A"]["year"] == 2001


async def test_CHANGESET_3_short_form_without_current_fails_the_step(h: Harness) -> None:
    run = h.run(wf("{id: p, uses: test.propose, with: {raw: {changes: [{field: year, new: 1}]}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert "current album or item" in str(run.steps[0].error)


async def test_CHANGESET_3_targets_outside_the_step_are_refused(h: Harness) -> None:
    other = h.backend.add_album({"album": "Not mine", "items": [{"title": "x"}]})
    raw = {
        "targets": [
            {
                "target": {"scope": "album", "id": other.id},
                "changes": [{"field": "album", "new": "Mine"}],
            }
        ]
    }
    run = h.run(wf("{id: p, uses: test.propose, with: {raw: " + _flow(raw) + "}}"))
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert "not one of this step's targets" in str(run.steps[0].error)
    assert h.engine.changesets.for_run(run.id) == []


def _flow(value: Any) -> str:
    import json

    return json.dumps(value)


async def test_CHANGESET_2_unappliable_proposals_fail_the_step_before_storing(
    h: Harness,
) -> None:
    run = h.run(wf("{id: p, uses: test.propose, with: {set: {year: soon}}}"))
    album(h, run, "A", year=2001)
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert "year" in str(run.steps[0].error)
    assert h.engine.changesets.for_run(run.id) == []
    assert "changeset.proposed" not in types(h, run)


# -- CHANGESET-4: no-ops ---------------------------------------------------------------


async def test_CHANGESET_4_no_op_changes_are_dropped(h: Harness) -> None:
    run = h.run(
        wf("{id: p, uses: test.propose, with: {set: {year: 2001, genre: Jazz}, delete: [mood]}}")
    )
    album(h, run, "A", year=2001, genre="Rock")
    await h.engine.execute(run)
    (stored,) = h.engine.changesets.for_run(run.id)
    assert [c.field for _, c in stored.changeset.field_changes()] == ["genre"]


async def test_CHANGESET_4_rerunning_an_applied_script_proposes_nothing(h: Harness) -> None:
    src = wf(
        "{id: p, uses: test.propose, with: {set: {year: 2002}}}",
        "{id: after, uses: test.echo, with: {message: done}}",
    )
    first = h.run(src)
    album(h, first, "A", year=2002)
    await h.engine.execute(first)
    assert first.state is RunState.succeeded
    assert first.steps[0].outputs == {"proposed": 0}
    assert "run.waiting_review" not in types(h, first)


# -- CHANGESET-14: one ChangeSet per album --------------------------------------------


async def test_CHANGESET_14_proposal_is_split_per_album_in_order(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: p, uses: test.propose, with: {whole_run: true, set: {label: L},"
            " item_set: {mood: calm}, rationale: tidy}}"
        )
    )
    album(h, run, "A", items=[{"title": "a1", "track": 1}, {"title": "a2", "track": 2}])
    album(h, run, "B")
    await h.engine.execute(run)
    assert run.steps[0].outputs == {"proposed": 2}
    a, b = (s.changeset for s in h.engine.changesets.for_run(run.id))
    assert [t.target.scope for t in a.targets] == ["album", "item", "item"]
    assert [t.target.scope for t in b.targets] == ["album", "item"]
    assert a.rationale == b.rationale == "tidy"
    assert a.id != b.id


# -- CHANGESET-5, -6: states, events, apply policy ------------------------------------


async def test_CHANGESET_6_auto_applies_each_changeset_when_proposed(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: p, uses: test.propose, apply: auto, with: {set: {label: L}}}",
            "{id: q, uses: test.echo, with: {message: next}}",
        )
    )
    album(h, run, "A")
    album(h, run, "B")
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert {s.state for s in h.engine.changesets.for_run(run.id)} == {"applied"}
    assert types(h, run).count("changeset.proposed") == 2
    assert types(h, run).count("changeset.applied") == 2
    assert [library(h)[n]["label"] for n in ("A", "B")] == ["L", "L"]
    applied = data(h, run, "changeset.applied")[0]
    assert applied["partial"] is False
    assert h.engine.journal.get(str(applied["journal_id"])) is not None


async def test_CHANGESET_5_events_are_on_the_run_stream_with_the_step(h: Harness) -> None:
    run = h.run(wf("{id: p, uses: test.propose, apply: auto, with: {set: {label: L}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    events = [e for e in h.events(run) if e.type.startswith("changeset.")]
    assert [(e.type, e.step_id) for e in events] == [
        ("changeset.proposed", "p"),
        ("changeset.applied", "p"),
    ]
    proposed = events[0].data
    assert proposed["field_changes"] == 1
    assert proposed["targets"] == [
        f"album {h.engine.changesets.for_run(run.id)[0].changeset.targets[0].target.id}"
    ]


async def test_CHANGESET_6_review_is_the_default_and_leaves_changesets_proposed(
    h: Harness,
) -> None:
    run = h.run(wf("{id: p, uses: test.propose, with: {set: {label: L}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    (stored,) = h.engine.changesets.for_run(run.id)
    assert stored.state == "proposed"
    assert "label" not in library(h)["A"]


def test_CHANGESET_6_force_review_makes_apply_auto_a_validation_error(h: Harness) -> None:
    with pytest.raises(RunValidationError, match="always waits for review"):
        h.run(wf("{id: p, uses: test.propose-reviewed, apply: auto}"))


async def test_CHANGESET_6_force_review_step_waits_for_review(h: Harness) -> None:
    run = h.run(wf("{id: p, uses: test.propose-reviewed, with: {set: {label: L}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    assert [s.state for s in h.engine.changesets.for_run(run.id)] == ["proposed"]


async def test_CHANGESET_10_write_failures_are_shown_in_the_run_log(h: Harness) -> None:
    run = h.run(wf("{id: p, uses: test.propose, apply: auto, with: {set: {label: L}}}"))
    album(h, run, "A")
    item = h.backend.query(Selection())[0].items[0]
    h.backend.fail_writes[item.id] = "permission denied"
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert h.logs(run, "error") == [f"{item.path}: tags not written: permission denied"]
    assert data(h, run, "changeset.applied")[0]["write_failures"] == [
        {"target": f"item {item.id}", "path": item.path, "error": "permission denied"}
    ]


# -- JOURNAL-1: write-ahead ------------------------------------------------------------


class _WatchingBackend(FakeBackend):
    """Checks that the journal holds a pending entry whenever apply runs."""

    def __init__(self, h: Harness) -> None:
        super().__init__()
        self.h = h
        self.pending_at_apply: list[int] = []

    def apply(self, changes: ChangeSet) -> ApplyResult:
        self.pending_at_apply.append(len(self.h.engine.journal.pending("main")))
        return super().apply(changes)


async def test_JOURNAL_1_entry_is_pending_before_apply_and_completed_after(
    h: Harness,
) -> None:
    h.backend = backend = _WatchingBackend(h)
    run = h.run(wf("{id: p, uses: test.propose, apply: auto, with: {set: {label: L}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    assert backend.pending_at_apply == [1]
    assert h.engine.journal.pending("main") == []
    (entry,) = h.engine.journal.for_run(run.id)
    assert entry.state == "applied"
    assert [e.field for e in entry.effects] == ["label", "label"]  # album + its item


async def test_JOURNAL_2_conflicts_complete_the_entry_as_failed(h: Harness) -> None:
    run = h.run(
        wf("{id: p, uses: test.propose, apply: auto, with: {set: {year: 2002}, old: {year: 1}}}")
    )
    album(h, run, "A", year=2001)
    await h.engine.execute(run)
    (entry,) = h.engine.journal.for_run(run.id)
    assert entry.state == "failed"
    assert [c.current for c in entry.conflicts] == [2001]
    assert data(h, run, "changeset.conflicted")[0]["conflicts"] == [
        {
            "target": f"album {entry.intended[0].target.id}",
            "field": "year",
            "expected": 1,
            "current": 2001,
        }
    ]


# -- CHANGESET-7 / RUN-14: suspend and resume -----------------------------------------


REVIEW_THEN_ECHO = wf(
    "{id: p, uses: test.propose, with: {set: {label: L}}}",
    "{id: after, uses: test.echo, with: {message: resumed}}",
)


async def test_CHANGESET_7_review_suspends_the_run_after_the_step(h: Harness) -> None:
    run = h.run(REVIEW_THEN_ECHO)
    album(h, run, "A")
    album(h, run, "B")
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    assert run.suspended_after == "p"
    ids = [s.changeset.id for s in h.engine.changesets.for_run(run.id)]
    assert run.holds == ids
    assert types(h, run)[-2:] == ["run.step.finished", "run.waiting_review"]
    assert data(h, run, "run.waiting_review") == [{"after": "p", "changesets": ids, "gates": []}]
    assert run.steps[1].status == "pending"
    assert "run.finished" not in types(h, run)


async def test_CHANGESET_7_suspends_even_after_the_last_step(h: Harness) -> None:
    run = h.run(wf("{id: p, uses: test.propose, with: {set: {label: L}}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review


async def test_RUN_14_resume_continues_after_the_suspending_step(h: Harness) -> None:
    src = wf(
        "{id: p, uses: test.propose, with: {set: {label: L}}}",
        "{id: after, uses: test.echo, with: {message: 'proposed {{ steps.p.proposed }}'}}",
    )
    first = h.run(src)
    album(h, first, "A")
    await h.engine.execute(first)
    assert first.state is RunState.waiting_review
    # A fresh engine (a new worker job) resumes the run from its stored records.
    resumed_engine = Harness()
    resumed_engine.backend = h.backend
    resumed_engine.engine.sink = resumed_engine.sink = h.sink  # the app DB, in the worker
    resumed = resumed_engine.run(src)
    assert resumed.id == first.id  # both harnesses hand out the same first id
    resumed.last_seq = first.last_seq
    resumed_engine.engine.restore_steps(resumed, {"p": first.steps[0]})
    await resumed_engine.engine.execute(resumed, resume_after="p")
    assert resumed.state is RunState.succeeded
    assert resumed_engine.logs(resumed) == ["proposed 1"]
    events = list(h.sink.since(first.id, first.last_seq))
    assert events[0].type == "run.resumed"
    assert events[0].seq == first.last_seq + 1
    assert [e.type for e in events][-1] == "run.finished"


def test_RUN_14_waiting_review_transitions() -> None:
    from rifferr.engine import InvalidTransitionError
    from tests.engine.test_run import TWO

    h = Harness()
    run = h.run(TWO)
    run.transition(RunState.running)
    run.transition(RunState.waiting_review)
    run.transition(RunState.queued)
    run.transition(RunState.running)
    run.transition(RunState.waiting_review)
    run.transition(RunState.cancelled)
    with pytest.raises(InvalidTransitionError):
        run.transition(RunState.running)
    other = h.run(TWO)
    with pytest.raises(InvalidTransitionError):
        other.transition(RunState.waiting_review)


async def test_CHANGESET_6_backend_without_apply_fails_proposing_steps(h: Harness) -> None:
    h.backend = FakeBackend(capabilities=BackendCapabilities(query=True))
    with pytest.raises(RunValidationError, match="apply"):
        h.run(wf("{id: p, uses: test.propose, with: {set: {label: L}}}"))
