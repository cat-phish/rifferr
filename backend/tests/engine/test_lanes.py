"""C-LANES: per-album lanes through a run (engine side)."""

from __future__ import annotations

from typing import Any

from rifferr.backends.base import RUN_MARKER
from rifferr.engine import ENGINE_EVENTS, Engine, Run, RunState, StepTypeRegistry, counts
from tests.engine.conftest import Harness
from tests.engine.steps import ALL


def wf(*steps: str) -> str:
    return "name: lanes\nsteps:\n" + "".join(f"  - {s}\n" for s in steps)


def album(h: Harness, run: Run, name: str) -> str:
    return h.backend.add_album(
        {"album": name, "albumartist": "Artist", RUN_MARKER: run.id, "items": [{"title": "t"}]}
    ).id


def lanes(h: Harness, run: Run) -> dict[str, tuple[str, str | None]]:
    return {
        lane.label: (lane.state, lane.position) for lane in h.engine.lanes.load(run.id).values()
    }


def data(h: Harness, run: Run, type: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.events(run) if e.type == type]


def accept_all(h: Harness, run: Run) -> None:
    for stored in h.engine.changesets.for_run(run.id, "proposed"):
        h.engine.changesets.set_state(stored.changeset.id, "applied", resolved_by="test")


async def resume(h: Harness, run: Run, src: str) -> Run:
    """What the worker does on a resume job: a fresh engine over the same stores."""
    engine = Engine(
        StepTypeRegistry(ALL),
        h.sink,
        clock=h.clock,
        new_id=lambda: run.id,
        journal=h.engine.journal,
        changesets=h.engine.changesets,
        gates=h.engine.gates,
        lanes=h.engine.lanes,
    )
    import textwrap

    from rifferr.core.profile import Redactor
    from rifferr.core.workflow import load_workflow

    text = textwrap.dedent(src)
    workflow, doc = load_workflow(text, "wf.yaml")
    again = engine.create_run(
        workflow,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=h.backend,
        doc=doc,
        redactor=Redactor([]),
        last_seq=run.last_seq,
    )
    engine.restore_steps(again, {r.id: r for r in run.steps if r.passes})
    assert run.suspended_after is not None
    await engine.execute(again, resume_after=run.suspended_after)
    return again


# Workflow from the spec's example: C needs review, A and B go on.
REVIEW_C = "{id: comp, uses: test.propose, when: 'album:C', with: {set: {label: L}}}"
SEEN = "{id: seen, uses: test.seen}"
ECHO_SEEN = "{id: after, uses: test.echo, with: {message: 'seen {{ steps.seen.seen }}'}}"
EXAMPLE = wf(REVIEW_C, SEEN, ECHO_SEEN)


async def three(h: Harness, src: str) -> Run:
    run = h.run(src)
    for name in ("A", "B", "C"):
        album(h, run, name)
    await h.engine.execute(run)
    return run


# -- LANES-1, -2 -------------------------------------------------------------------


async def test_LANES_1_a_lane_per_run_album_keyed_by_album_id(h: Harness) -> None:
    run = h.run(wf(SEEN))
    ids = [album(h, run, n) for n in ("A", "B")]
    h.backend.add_album({"album": "Other", RUN_MARKER: "another-run", "items": []})
    await h.engine.execute(run)
    stored = h.engine.lanes.load(run.id)
    assert sorted(stored) == sorted(ids)
    assert {lane.label for lane in stored.values()} == {"Artist - A", "Artist - B"}


async def test_LANES_1_late_album_starts_at_the_step_where_it_appears(h: Harness) -> None:
    from pathlib import Path

    h.backend.add_import_fixture(Path("/in"), [{"album": "Late"}])
    run = h.run(
        wf(SEEN, "{id: imp, uses: test.import, with: {path: /in}}", "{id: s2, uses: test.seen}")
    )
    album(h, run, "A")
    await h.engine.execute(run)
    assert h.logs(run) == ["seen: A", "seen: A,Late"]
    started = data(h, run, "lane.started")
    assert [(d["label"], d["position"]) for d in started] == [
        ("Artist - A", "seen"),
        ("Late", "s2"),
    ]


async def test_LANES_2_events_for_every_lane_state_change(h: Harness) -> None:
    assert {"lane.started", "lane.waiting", "lane.resumed", "lane.finished"} <= ENGINE_EVENTS
    run = await three(h, EXAMPLE)
    waiting = data(h, run, "lane.waiting")
    assert [(d["label"], [x["kind"] for x in d["holds"]]) for d in waiting] == [
        ("Artist - C", ["changeset"])
    ]
    assert sorted((d["label"], d["state"]) for d in data(h, run, "lane.finished")) == [
        ("Artist - A", "done"),
        ("Artist - B", "done"),
    ]
    accept_all(h, run)
    resumed = await resume(h, run, EXAMPLE)
    events = [e for e in h.sink.since(run.id, run.last_seq)]
    assert [e.data["label"] for e in events if e.type == "lane.resumed"] == ["Artist - C"]
    assert resumed.state is RunState.succeeded


# -- LANES-3, -4, -5, -7 (the spec's example) --------------------------------------------


async def test_LANES_3_held_album_waits_while_the_others_finish(h: Harness) -> None:
    run = await three(h, EXAMPLE)
    assert run.state is RunState.waiting_review  # LANES-7: no active lane, one waiting
    assert lanes(h, run) == {
        "Artist - A": ("done", None),
        "Artist - B": ("done", None),
        "Artist - C": ("waiting", "seen"),
    }
    assert h.logs(run) == ["seen: A,B", "seen 2"]


async def test_LANES_3_a_pass_with_no_lanes_does_not_run(h: Harness) -> None:
    run = h.run(
        wf("{id: p, uses: test.propose, with: {set: {x: '1'}}}", "{id: each, uses: test.per-album}")
    )
    album(h, run, "A")
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    assert ("run.step.started", "each") not in h.types(run)


async def test_LANES_4_one_record_per_pass_and_outputs_follow_the_lane(h: Harness) -> None:
    run = await three(h, EXAMPLE)
    accept_all(h, run)
    resumed = await resume(h, run, EXAMPLE)
    assert resumed.state is RunState.succeeded
    seen = next(r for r in resumed.steps if r.id == "seen")
    assert [(p.number, p.lanes, p.outputs) for p in seen.passes] == [
        (1, 2, {"seen": 2}),
        (2, 1, {"seen": 1}),
    ]
    finished = [e for e in h.events(run) if e.type == "run.step.finished" and e.step_id == "seen"]
    assert [(e.data["pass"], e.data["lanes"]) for e in finished] == [(1, 2), (2, 1)]
    assert h.logs(resumed)[-2:] == ["seen: C", "seen 1"]  # C's own pass of `seen`


async def test_LANES_5_gate_holds_only_its_albums(h: Harness) -> None:
    src = wf("{id: check, uses: review, when: 'album:B', with: {message: 'look'}}", SEEN)
    from rifferr.steps.review import ReviewStep

    h.engine.registry = StepTypeRegistry([*ALL, ReviewStep()])
    run = await three(h, src)
    assert lanes(h, run)["Artist - B"] == ("waiting", "seen")
    assert [line for line in h.logs(run) if line.startswith("seen")] == ["seen: A,C"]


async def test_LANES_5_ctx_hold_holds_only_that_album(h: Harness) -> None:
    run = await three(h, wf("{id: hold, uses: test.hold, with: {albums: [A]}}", SEEN))
    assert lanes(h, run)["Artist - A"] == ("waiting", "seen")
    assert h.logs(run) == ["seen: B,C"]
    assert run.state is RunState.waiting_review


# -- LANES-6 ------------------------------------------------------------------------


async def test_LANES_6_run_level_step_runs_before_any_lane(h: Harness) -> None:
    from pathlib import Path

    h.backend.add_import_fixture(Path("/in"), [{"album": "A"}, {"album": "B"}])
    run = h.run(wf("{id: imp, uses: test.import, with: {path: /in}}", SEEN))
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert h.logs(run) == ["seen: A,B"]
    first = data(h, run, "run.step.started")[0]
    assert (first["pass"], first["lanes"]) == (1, 0)


async def test_LANES_6_shell_per_run_is_run_level() -> None:
    from rifferr.engine.steps import is_run_level
    from rifferr.steps.beets_import import BeetsImportStep
    from rifferr.steps.shell import ShellStep

    assert is_run_level(BeetsImportStep(), {})
    assert is_run_level(ShellStep(), {"per": "run"})
    assert not is_run_level(ShellStep(), {"per": "album"})
    assert not is_run_level(ShellStep(), {})


async def test_LANES_6_run_level_step_skips_when_all_lanes_wait(h: Harness) -> None:
    run = await three(h, wf("{id: hold, uses: test.hold, with: {albums: [A, B, C]}}", SEEN))
    assert ("run.step.started", "seen") not in h.types(run)


# -- LANES-8 ------------------------------------------------------------------------


async def test_LANES_8_only_released_lanes_resume(h: Harness) -> None:
    src = wf("{id: hold, uses: test.hold, with: {albums: [A, B]}}", SEEN)
    run = await three(h, src)
    h.engine.resolve_hold(run.id, "hold-A")
    resumed = await resume(h, run, src)
    assert resumed.state is RunState.waiting_review  # B still waits
    assert h.logs(resumed)[-1] == "seen: A"
    assert lanes(h, run)["Artist - B"] == ("waiting", "seen")
    h.engine.resolve_hold(run.id, "hold-B")
    final = await resume(h, resumed, src)
    assert final.state is RunState.succeeded
    assert h.logs(final)[-1] == "seen: B"


async def test_LANES_8_resume_starts_at_the_earliest_released_position(h: Harness) -> None:
    src = wf(
        "{id: h1, uses: test.hold, with: {albums: [A]}}",
        "{id: h2, uses: test.hold, with: {albums: [B]}}",
        SEEN,
    )
    run = await three(h, src)
    assert lanes(h, run)["Artist - A"] == ("waiting", "h2")
    assert lanes(h, run)["Artist - B"] == ("waiting", "seen")
    h.engine.resolve_hold(run.id, "hold-A")
    h.engine.resolve_hold(run.id, "hold-B")
    resumed = await resume(h, run, src)
    assert resumed.state is RunState.succeeded
    # A runs h2 alone, then A and B meet at `seen` in one pass.
    assert h.logs(resumed)[-1] == "seen: A,B"


# -- LANES-9, -10 -------------------------------------------------------------------


async def test_LANES_9_any_failed_lane_fails_the_run_and_counts_by_state(h: Harness) -> None:
    run = await three(h, wf("{id: each, uses: test.per-album, with: {fail_on: [B]}}", SEEN))
    assert run.state is RunState.failed
    assert counts(h.engine.lanes.load(run.id).values()) == {"done": 2, "failed": 1}
    assert h.logs(run)[-1] == "seen: A,C"


async def test_LANES_10_failure_without_target_under_stop_fails_every_lane(h: Harness) -> None:
    run = h.run(wf("{id: e, uses: test.echo, with: {message: m, fail: true}}", SEEN))
    for name in ("A", "B"):
        album(h, run, name)
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert {s for s, _ in lanes(h, run).values()} == {"failed"}
    assert "seen: A,B" not in h.logs(run)


async def test_LANES_10_failure_without_target_under_skip_moves_lanes_on(h: Harness) -> None:
    run = h.run(
        wf("{id: e, uses: test.echo, on_error: skip, with: {message: m, fail: true}}", SEEN)
    )
    for name in ("A", "B"):
        album(h, run, name)
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert h.logs(run)[-1] == "seen: A,B"


# -- LANES-12, -13, -14 ----------------------------------------------------------


async def test_LANES_12_album_gone_ends_its_lane_gone(h: Harness) -> None:
    run = await three(h, wf("{id: rm, uses: test.remove, with: {albums: [B]}}", SEEN))
    assert run.state is RunState.succeeded  # gone is not a failure
    assert lanes(h, run)["Artist - B"] == ("gone", None)
    assert h.logs(run) == ["seen: A,C"]


async def test_LANES_13_cancel_ends_open_lanes_cancelled(h: Harness) -> None:
    import asyncio

    run = h.run(wf("{id: slow, uses: test.slow, with: {cooperative: true}}", SEEN))
    album(h, run, "A")
    task = asyncio.create_task(h.engine.execute(run))
    await asyncio.sleep(0.05)
    h.engine.cancel(run.id)
    await task
    assert run.state is RunState.cancelled
    assert lanes(h, run) == {"Artist - A": ("cancelled", None)}


async def test_LANES_14_ctx_hold_resolves_through_the_engine(h: Harness) -> None:
    src = wf("{id: hold, uses: test.hold, with: {albums: [A]}}", SEEN)
    run = h.run(src)
    album(h, run, "A")
    await h.engine.execute(run)
    (lane,) = h.engine.lanes.load(run.id).values()
    assert [(x.kind, x.ref) for x in lane.holds] == [("import", "hold-A")]
    h.engine.resolve_hold(run.id, "hold-A")
    resumed = await resume(h, run, src)
    assert resumed.state is RunState.succeeded


def test_LANES_14_pending_lane_is_renamed_keeping_its_history() -> None:
    from rifferr.engine import InMemoryLaneStore, Lane

    store = InMemoryLaneStore()
    store.save("r", [Lane("pending:1", "Folder", "waiting", "tag", steps={"imp": "succeeded"})])
    assert Lane("pending:1", "x").pending
    store.rename("r", "pending:1", "album-7")
    (lane,) = store.load("r").values()
    assert (lane.key, lane.position, lane.steps) == ("album-7", "tag", {"imp": "succeeded"})
