"""S-REVIEW in the engine: the ``review`` step and gate creation (REVIEW-1 .. -5, -12)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER, BackendCapabilities, TemplateError
from rifferr.backends.fake import FakeBackend
from rifferr.core.duration import parse_duration
from rifferr.core.review import ReviewGate
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow, validate
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.review import InMemoryGateStore, fold_changes
from rifferr.engine.undo import UndoContext, undo_entry
from rifferr.steps.review import ReviewParams, ReviewStep
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = StepTypeRegistry([*ALL, ReviewStep()])


def wf(*steps: str) -> str:
    return "name: gate\nsteps:\n" + "".join(f"  - {s}\n" for s in steps)


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.engine.registry = REGISTRY
    return harness


def album(h: Harness, run: Run, name: str, **fields: Any) -> None:
    items = fields.pop("items", [{"title": f"{name} 1", "track": 1}])
    h.backend.add_album(
        {"albumartist": "Artist", "album": name, RUN_MARKER: run.id, **fields, "items": items}
    )


def types(h: Harness, run: Run) -> list[str]:
    return [t for t, _ in h.types(run)]


def data(h: Harness, run: Run, type: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.events(run) if e.type == type]


def gates(h: Harness) -> list[ReviewGate]:
    store = h.engine.gates
    assert isinstance(store, InMemoryGateStore)
    return list(store.items.values())


def errors(src: str) -> list[str]:
    workflow, doc = load_workflow(src, "wf.yaml")
    return [str(e) for e in validate(workflow, REGISTRY, None, {}, doc=doc)]


GATE = "{id: check, uses: review, with: {message: 'Check $albumartist - $album'}}"
THEN = "{id: after, uses: test.echo, with: {message: 'after the gate'}}"


# -- REVIEW-1: parameters ----------------------------------------------------------


def test_REVIEW_1_parameters_and_defaults() -> None:
    p = ReviewParams.model_validate({})
    assert (p.message, p.show, p.on_reject, p.timeout, p.on_timeout) == (
        None,
        [],
        "stop",
        None,
        None,
    )
    full = ReviewParams.model_validate(
        {
            "message": "m",
            "show": ["album", "year"],
            "on_reject": "undo",
            "timeout": "3d",
            "on_timeout": "continue",
        }
    )
    assert full.on_reject == "undo"
    assert full.timeout == "3d"


def test_REVIEW_1_on_timeout_without_timeout_is_a_validation_error() -> None:
    problems = errors(wf("{id: check, uses: review, with: {on_timeout: continue}}"))
    assert any("on_timeout needs a timeout" in p for p in problems)
    assert errors(wf("{id: check, uses: review, with: {timeout: 12h, on_timeout: reject}}")) == []


@pytest.mark.parametrize("bad", ["3", "3 days", "0h", "-1d", "d", ""])
def test_REVIEW_1_bad_timeouts_are_validation_errors(bad: str) -> None:
    problems = errors(wf(f"{{id: check, uses: review, with: {{timeout: '{bad}'}}}}"))
    assert problems, bad


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("12h", timedelta(hours=12)),
        ("3d", timedelta(days=3)),
        ("1d12h", timedelta(hours=36)),
        ("90s", timedelta(seconds=90)),
    ],
)
def test_REVIEW_1_durations(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


async def test_REVIEW_1_message_is_interpolated_before_it_is_a_template(h: Harness) -> None:
    src = (
        "name: gate\nvars: {who: {default: Ana}}\nsteps:\n"
        "  - {id: check, uses: review, with: {message: '{{ vars.who }}: check $album'}}\n"
    )
    run = h.run(src)
    album(h, run, "A")
    await h.engine.execute(run)
    (gate,) = gates(h)
    assert gate.albums[0].message == "Ana: check A"


# -- REVIEW-2: selection -------------------------------------------------------------


async def test_REVIEW_2_nothing_to_review_finishes_without_pausing(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, when: 'comp:1'}", THEN))
    album(h, run, "A", comp=0)
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert "nothing to review" in h.logs(run)
    assert "after the gate" in h.logs(run)
    assert gates(h) == []
    assert "review.requested" not in types(h, run)


async def test_REVIEW_2_gate_covers_only_albums_matching_when(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, when: 'comp:1'}", THEN))
    album(h, run, "Solo", comp=0)
    album(h, run, "Hits", comp=1)
    await h.engine.execute(run)
    (gate,) = gates(h)
    assert [a.label for a in gate.albums] == ["Artist - Hits"]


async def test_REVIEW_2_gate_ignores_albums_of_other_runs(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review}"))
    h.backend.add_album({"album": "Old", RUN_MARKER: "another-run", "items": [{"title": "t"}]})
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert gates(h) == []


# -- REVIEW-3: gate creation and suspension -------------------------------------------


async def test_REVIEW_3_gate_holds_each_album_and_suspends_the_run(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: check, uses: review, with: {message: 'Check $album', show: [album, year]}}",
            THEN,
        )
    )
    album(h, run, "A", year=2001)
    album(h, run, "B", year=2002)
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    assert run.suspended_after == "check"
    (gate,) = gates(h)
    assert (gate.run_id, gate.step_id, gate.state, gate.profile) == (
        run.id,
        "check",
        "pending",
        "main",
    )
    assert [(a.label, a.message, a.shown) for a in gate.albums] == [
        ("Artist - A", "Check A", {"album": "A", "year": 2001}),
        ("Artist - B", "Check B", {"album": "B", "year": 2002}),
    ]
    assert all(a.imported_by_run for a in gate.albums)
    assert gate.deadline is None
    assert run.gates == [gate.id]
    assert run.holds == []
    assert data(h, run, "review.requested") == [{"gate_id": gate.id, "albums": 2}]
    assert data(h, run, "run.waiting_review") == [
        {"after": "check", "changesets": [], "gates": [gate.id]}
    ]
    order = types(h, run)
    assert order.index("review.requested") < order.index("run.step.finished")
    assert order[-1] == "run.waiting_review"
    assert "after the gate" not in h.logs(run)
    finished = data(h, run, "run.step.finished")[-1]
    assert finished["status"] == "succeeded"
    assert finished["outputs"] == {"gate": gate.id, "albums": 2}


async def test_REVIEW_3_shown_fields_missing_on_the_album_are_none(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, with: {show: [label]}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    (gate,) = gates(h)
    assert gate.albums[0].shown == {"label": None}
    assert gate.albums[0].message is None


async def test_REVIEW_3_timeout_sets_a_deadline_and_defaults_to_reject(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, with: {timeout: 2h}}"))
    album(h, run, "A")
    await h.engine.execute(run)
    (gate,) = gates(h)
    assert gate.deadline is not None
    assert gate.deadline - gate.created_at == timedelta(hours=2)
    assert gate.on_timeout == "reject"
    assert data(h, run, "review.requested")[0]["deadline"] == gate.deadline.isoformat()


async def test_REVIEW_3_options_reach_the_gate(h: Harness) -> None:
    run = h.run(
        wf("{id: check, uses: review, with: {on_reject: undo, timeout: 1d, on_timeout: continue}}")
    )
    album(h, run, "A")
    await h.engine.execute(run)
    (gate,) = gates(h)
    assert (gate.on_reject, gate.on_timeout) == ("undo", "continue")


# -- REVIEW-4: changes so far -----------------------------------------------------------


async def test_REVIEW_4_changes_so_far_fold_the_runs_journal(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: one, uses: test.propose, apply: auto, with: {set: {album: Hits2, label: X}}}",
            "{id: two, uses: test.propose, apply: auto, with: {set: {album: Hits3, label: Y}}}",
            "{id: back, uses: test.propose, apply: auto, with: {set: {label: L0}}}",
            "{id: items, uses: test.propose, apply: auto, with: {item_set: {title: New}}}",
            GATE,
        )
    )
    album(h, run, "Hits", label="L0")
    await h.engine.execute(run)
    (gate,) = gates(h)
    (gated,) = gate.albums
    changes = {(c.target.scope, c.field): (c.before, c.after) for c in gated.changes}
    # album: first before, last after; label ended where it started, so it's left out.
    assert changes[("album", "album")] == ("Hits", "Hits3")
    assert ("album", "label") not in changes
    assert changes[("item", "title")] == ("Hits 1", "New")
    assert gated.label == "Artist - Hits3"


async def test_REVIEW_4_undone_entries_are_left_out(h: Harness) -> None:
    run = h.run(wf("{id: one, uses: test.propose, apply: auto, with: {set: {year: 2002}}}"))
    album(h, run, "A", year=2001)
    await h.engine.execute(run)
    (entry,) = h.engine.journal.for_run(run.id)
    ctx = UndoContext(
        backend=h.backend,
        journal=h.engine.journal,
        store=h.engine.changesets,
        new_id=lambda: "01UNDO00000000000000000001",
        now=h.clock.now,
        emitter_for=lambda run_id, step_id: lambda type, data: None,
    )
    assert undo_entry(entry.id, ctx).undone
    snapshot = h.backend.query(Selection())[0]
    assert fold_changes(h.engine.journal.for_run(run.id), snapshot) == []


async def test_REVIEW_4_conflicted_changesets_are_listed(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: p, uses: test.propose, apply: auto, on_error: continue,"
            " with: {set: {year: 2002}, old: {year: 1999}}}",
            GATE,
        )
    )
    album(h, run, "A", year=2001)
    album(h, run, "B", year=1999)
    await h.engine.execute(run)
    conflicted = [s.changeset.id for s in h.engine.changesets.for_run(run.id, "conflicted")]
    assert len(conflicted) == 1
    by_name = {a.label: a for a in gates(h)[0].albums}
    assert by_name["Artist - A"].conflicted == conflicted
    assert by_name["Artist - B"].conflicted == []
    # The album's year, and the item's inherited copy of it.
    assert {(c.target.scope, c.field, c.after) for c in by_name["Artist - B"].changes} == {
        ("album", "year", 2002),
        ("item", "year", 2002),
    }


# -- REVIEW-5: template errors ------------------------------------------------------------


async def test_REVIEW_5_template_error_fails_the_album_under_continue(h: Harness) -> None:
    run = h.run(
        wf(
            "{id: check, uses: review, on_error: continue, with: {message: '%if{$comp,bad,fine}'}}",
        )
    )
    album(h, run, "A")
    await h.engine.execute(run)
    # FakeBackend refuses every template function, so the only album fails.
    assert run.state is RunState.succeeded
    assert gates(h) == []
    assert run.steps[0].status == "failed"
    assert len(run.steps[0].failed_targets) == 1


async def test_REVIEW_5_gate_is_created_for_the_other_albums(h: Harness) -> None:
    class PickyBackend(FakeBackend):
        def evaluate(self, template: str, target: Any) -> str:
            if target.fields.get("album") == "Bad":
                raise TemplateError("no such function")
            return super().evaluate(template, target)

    picky = PickyBackend.from_fixture({"albums": []})
    h.backend = picky
    run = h.run(wf("{id: check, uses: review, on_error: continue, with: {message: '$album'}}"))
    album(h, run, "Bad")
    album(h, run, "Good")
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    (gate,) = gates(h)
    assert [a.message for a in gate.albums] == ["Good"]
    assert [f.message for f in run.steps[0].failed_targets] == ["message: no such function"]


async def test_REVIEW_5_template_error_fails_the_step_under_stop(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, with: {message: '%upper{$album}'}}", THEN))
    album(h, run, "A")
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert gates(h) == []
    assert run.steps[1].status == "skipped"


# -- REVIEW-12: no library writes ------------------------------------------------------


def test_REVIEW_12_needs_only_query_and_templates() -> None:
    assert ReviewStep.requires == BackendCapabilities(query=True, templates=True)


async def test_REVIEW_12_the_gate_writes_nothing(h: Harness) -> None:
    run = h.run(wf("{id: check, uses: review, with: {show: [album]}}"))
    album(h, run, "A")
    before = h.backend.query(Selection())
    await h.engine.execute(run)
    after = h.backend.query(Selection())
    assert before == after
    assert list(h.engine.changesets.for_run(run.id)) == []
    assert h.engine.journal.for_run(run.id) == []
