from __future__ import annotations

import asyncio
import re
from datetime import UTC

import pytest

from rifferr.engine import (
    InvalidTransitionError,
    Run,
    RunState,
    RunValidationError,
)
from tests.engine.conftest import Harness
from tests.engine.steps import SlowStep

TWO = """\
name: two
steps:
  - {id: a, uses: test.echo, with: {message: hello}}
  - {id: b, uses: test.echo, with: {message: world}}
"""


# -- RUN-1 state machine -------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        (["running", "succeeded"], True),
        (["running", "failed"], True),
        (["running", "cancelled"], True),
        (["cancelled"], True),
        (["succeeded"], False),
        (["running", "queued"], False),
        (["running", "succeeded", "running"], False),
        (["running", "running"], False),
    ],
)
def test_RUN_1_state_transitions(h: Harness, path: list[str], ok: bool) -> None:
    run = h.run(TWO)
    assert run.state == RunState.queued
    if ok:
        for state in path:
            run.transition(RunState(state))
        assert run.state == RunState(path[-1])
    else:
        *ok_steps, bad = path
        for state in ok_steps:
            run.transition(RunState(state))
        with pytest.raises(InvalidTransitionError):
            run.transition(RunState(bad))


# -- RUN-2 order, interpolation, selection -------------------------------------


async def test_RUN_2_steps_run_in_file_order(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    assert h.logs(run) == ["hello", "world"]
    assert run.state == RunState.succeeded


async def test_RUN_2_with_values_are_interpolated(h: Harness) -> None:
    run = h.run(
        """\
        name: interp
        vars:
          greeting: { default: hi }
          who: {}
        steps:
          - {id: a, uses: test.echo, with: {message: "{{ vars.greeting }} {{ vars.who }}"}}
          - id: b
            uses: test.echo
            with: {message: "{{ run.id }} {{ run.workflow }} {{ profile.name }}"}
          - {id: c, uses: test.echo, with: {message: "$album stays"}}
        """,
        profile_vars={"who": "profile"},
    )
    await h.engine.execute(run)
    assert h.logs(run) == ["hi profile", f"{run.id} interp main", "$album stays"]


async def test_RUN_2_run_vars_override_profile_and_defaults(h: Harness) -> None:
    run = h.run(
        "name: x\nvars: {who: {default: d}}\n"
        "steps: [{id: a, uses: test.echo, with: {message: '{{ vars.who }}'}}]\n",
        profile_vars={"who": "p"},
        run_vars={"who": "cli"},
    )
    await h.engine.execute(run)
    assert h.logs(run) == ["cli"]


async def test_RUN_2_default_selection_is_albums_touched_by_this_run(h: Harness) -> None:
    h.backend.add_album({"album": "Old", "rifferr_run": "someone-else"})
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: imp, uses: test.import, with: {path: /in}}\n"
        "  - {id: each, uses: test.per-album}\n"
    )
    from pathlib import Path

    h.backend.add_import_fixture(Path("/in"), [{"album": "New A"}, {"album": "New B"}])
    await h.engine.execute(run)
    assert h.logs(run) == ["processed New A", "processed New B"]


async def test_RUN_2_when_restricts_targets(h: Harness) -> None:
    from pathlib import Path

    h.backend.add_import_fixture(
        Path("/in"), [{"album": "EP One", "albumtype": "ep"}, {"album": "LP", "albumtype": "album"}]
    )
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: imp, uses: test.import, with: {path: /in}}\n"
        "  - {id: eps, uses: test.per-album, when: 'albumtype:ep'}\n"
    )
    await h.engine.execute(run)
    assert h.logs(run) == ["processed EP One"]


# -- RUN-4 / RUN-12 step results and outputs -----------------------------------


async def test_RUN_12_outputs_feed_later_steps(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: a, uses: test.echo, with: {outputs: {count: 3, ok: true, label: hi}}}\n"
        "  - {id: b, uses: test.echo, with: {message: '{{ steps.a.count }}/{{ steps.a.ok }}/"
        "{{ steps.a.label }}'}}\n"
    )
    await h.engine.execute(run)
    assert h.logs(run)[-1] == "3/true/hi"
    assert run.steps[0].outputs == {"count": 3, "ok": True, "label": "hi"}


async def test_RUN_12_missing_output_fails_the_referencing_step(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: a, uses: test.echo}\n"
        "  - {id: b, uses: test.echo, with: {message: '{{ steps.a.nope }}'}}\n"
    )
    await h.engine.execute(run)
    assert run.state == RunState.failed
    assert run.steps[1].status == "failed"
    assert "nope" in (run.steps[1].error or "")


async def test_RUN_4_params_are_validated_after_interpolation(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n  - {id: a, uses: test.echo}\n"
        "  - {id: b, uses: test.echo, with: {message: '{{ steps.a.x }}'}}\n"
    )
    await h.engine.execute(run)
    assert run.steps[1].status == "failed"


# -- RUN-5 on_error ------------------------------------------------------------


async def test_RUN_5_stop_fails_run_and_skips_rest(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: a, uses: test.echo, with: {message: m, fail: true}}\n"
        "  - {id: b, uses: test.echo, with: {message: never}}\n"
    )
    await h.engine.execute(run)
    assert run.state == RunState.failed
    assert [s.status for s in run.steps] == ["failed", "skipped"]
    assert "never" not in h.logs(run)
    assert run.steps[0].error == "echo failed: m"


async def test_RUN_5_skip_continues_with_next_step(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: a, uses: test.echo, on_error: skip, with: {message: m, fail: true}}\n"
        "  - {id: b, uses: test.echo, with: {message: after}}\n"
    )
    await h.engine.execute(run)
    assert run.state == RunState.succeeded
    assert [s.status for s in run.steps] == ["failed", "succeeded"]
    assert h.types(run) == [
        ("run.started", None),
        ("run.step.started", "a"),
        ("run.step.log", "a"),
        ("run.step.finished", "a"),
        ("run.step.started", "b"),
        ("run.step.log", "b"),
        ("run.step.finished", "b"),
        ("run.finished", None),
    ]


async def _per_album_run(h: Harness, on_error: str) -> Run:
    from pathlib import Path

    h.backend.add_import_fixture(Path("/in"), [{"album": n} for n in ("A", "B", "C")])
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: imp, uses: test.import, with: {path: /in}}\n"
        f"  - {{id: each, uses: test.per-album, on_error: {on_error}, with: {{fail_on: [B]}}}}\n"
        "  - {id: last, uses: test.echo, with: {message: end}}\n"
    )
    await h.engine.execute(run)
    return run


async def test_RUN_5_continue_processes_remaining_targets(h: Harness) -> None:
    run = await _per_album_run(h, "continue")
    assert h.logs(run) == ["processed A", "processed C", "end"]
    assert len(h.logs(run, "error")) == 1
    assert h.logs(run, "error")[0].endswith(": could not process B")
    each = run.steps[1]
    assert each.status == "failed"
    assert [f.message for f in each.failed_targets] == ["could not process B"]
    assert each.outputs == {"done": 2}
    assert run.state == RunState.succeeded


async def test_RUN_5_target_failure_under_stop_ends_step_and_run(h: Harness) -> None:
    run = await _per_album_run(h, "stop")
    assert h.logs(run) == ["processed A"]
    assert run.state == RunState.failed
    assert [s.status for s in run.steps] == ["succeeded", "failed", "skipped"]


async def test_RUN_5_target_failure_under_skip_ends_step_only(h: Harness) -> None:
    run = await _per_album_run(h, "skip")
    assert h.logs(run) == ["processed A", "end"]
    assert run.state == RunState.succeeded


async def test_RUN_5_continue_without_targets_behaves_like_skip(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps:\n"
        "  - {id: a, uses: test.echo, on_error: continue, with: {message: m, fail: true}}\n"
        "  - {id: b, uses: test.echo, with: {message: after}}\n"
    )
    await h.engine.execute(run)
    assert [s.status for s in run.steps] == ["failed", "succeeded"]


async def test_RUN_5_unexpected_exception_is_a_step_failure(h: Harness) -> None:
    run = h.run("name: x\nsteps: [{id: a, uses: test.crash}]\n")
    await h.engine.execute(run)
    assert run.state == RunState.failed
    assert run.steps[0].error == "RuntimeError: unexpected bug"
    finished = next(e for e in h.events(run) if e.type == "run.step.finished")
    assert finished.data["status"] == "failed"
    assert finished.data["error"] == "RuntimeError: unexpected bug"


# -- RUN-6 / RUN-7 events ------------------------------------------------------


async def test_RUN_6_event_envelope(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    events = h.events(run)
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.run_id == run.id for e in events)
    assert all(e.ts.tzinfo is UTC for e in events)
    assert [e.ts for e in events] == sorted(e.ts for e in events)
    assert events[0].type == "run.started"
    assert events[0].data == {"workflow": "two", "profile": "main"}
    assert events[-1].type == "run.finished"
    assert events[-1].data["status"] == "succeeded"
    log = events[2]
    assert (log.type, log.step_id, log.data) == (
        "run.step.log",
        "a",
        {"stream": "info", "line": "hello"},
    )
    finished = events[3]
    assert finished.data == {"status": "succeeded", "outputs": {}, "failed_targets": []}


async def test_RUN_7_events_are_persisted_before_publication(h: Harness) -> None:
    seen: list[tuple[int, bool]] = []

    def listener(event: object) -> None:
        from rifferr.engine import Event

        assert isinstance(event, Event)
        seen.append((event.seq, any(e.seq == event.seq for e in h.sink.since(event.run_id, 0))))

    h.engine.subscribe(listener)
    run = h.run(TWO)
    await h.engine.execute(run)
    assert seen
    assert all(persisted for _, persisted in seen)


async def test_RUN_7_replay_after_seq_is_exactly_once(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    all_events = h.events(run)
    later = list(h.sink.since(run.id, 3))
    assert [e.seq for e in later] == [e.seq for e in all_events[3:]]
    assert list(h.sink.since(run.id, all_events[-1].seq)) == []
    assert list(h.sink.since("other-run", 0)) == []


async def test_RUN_7_sink_rejects_out_of_order_seq(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    with pytest.raises(ValueError, match="seq"):
        h.sink.append(h.events(run)[0])


async def test_RUN_6_step_log_lines_are_redacted(h: Harness) -> None:
    run = h.run(
        "name: x\nsteps: [{id: a, uses: test.echo, with: {message: 'token=s3cr3t-value ok'}}]\n",
        secrets=["s3cr3t-value"],
    )
    await h.engine.execute(run)
    assert h.logs(run) == ["token=*** ok"]


async def test_RUN_6_steps_may_not_emit_engine_events(h: Harness) -> None:
    from rifferr.engine import StepContext

    with pytest.raises(ValueError, match=r"run\.step\.finished"):
        StepContext.check_event_type("run.step.finished")
    with pytest.raises(ValueError, match=r"run\.step\."):
        StepContext.check_event_type("custom")
    StepContext.check_event_type("run.step.progress")


# -- RUN-8 cancellation --------------------------------------------------------


async def _start_slow(h: Harness, cooperative: bool) -> tuple[Run, asyncio.Task[None]]:
    SlowStep.started = asyncio.Event()
    run = h.run(
        "name: x\nsteps:\n"
        f"  - {{id: slow, uses: test.slow, with: {{cooperative: {str(cooperative).lower()}}}}}\n"
        "  - {id: after, uses: test.echo, with: {message: never}}\n"
    )
    task = asyncio.create_task(h.engine.execute(run))
    await asyncio.wait_for(SlowStep.started.wait(), 1)
    return run, task


async def test_RUN_8_cooperative_cancel(h: Harness) -> None:
    run, task = await _start_slow(h, cooperative=True)
    h.engine.cancel(run.id)
    await asyncio.wait_for(task, 1)
    assert run.state == RunState.cancelled
    assert [s.status for s in run.steps] == ["cancelled", "skipped"]
    assert h.logs(run) == ["stopping cleanly"]
    assert h.events(run)[-1].data == {"status": "cancelled"}


async def test_RUN_8_uncooperative_step_is_cancelled_after_grace(h: Harness) -> None:
    run, task = await _start_slow(h, cooperative=False)
    h.engine.cancel(run.id)
    await asyncio.wait_for(task, 1)
    assert run.state == RunState.cancelled
    assert run.steps[0].status == "cancelled"


async def test_RUN_8_cancel_before_start(h: Harness) -> None:
    run = h.run(TWO)
    h.engine.cancel(run.id)
    await h.engine.execute(run)
    assert run.state == RunState.cancelled
    assert h.logs(run) == []
    assert [e.type for e in h.events(run)] == ["run.finished"]


async def test_RUN_8_cancel_after_finish_is_a_no_op(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    h.engine.cancel(run.id)
    assert run.state == RunState.succeeded


# -- RUN-9 purity / determinism ------------------------------------------------


async def test_RUN_9_engine_is_deterministic_with_injected_clock_and_ids(h: Harness) -> None:
    run = h.run(TWO)
    await h.engine.execute(run)
    assert run.id == "01RUN" + "0" * 20 + "1"
    stamps = [e.ts for e in h.events(run)]
    assert stamps == sorted(set(stamps))
    assert all(ts.date().isoformat() == "2026-11-11" for ts in stamps)


# -- RUN-10 validation ---------------------------------------------------------


def test_RUN_10_invalid_workflow_never_becomes_a_run(h: Harness) -> None:
    with pytest.raises(RunValidationError) as exc:
        h.run("name: x\nsteps: [{id: a, uses: test.nope}]\n")
    assert "unknown step type 'test.nope'" in str(exc.value)
    assert h.engine.runs == {}


def test_RUN_10_missing_capability_is_a_validation_error(h: Harness) -> None:
    from rifferr.backends.base import BackendCapabilities
    from rifferr.backends.fake import FakeBackend

    h.backend = FakeBackend(capabilities=BackendCapabilities())
    with pytest.raises(RunValidationError, match="import"):
        h.run("name: x\nsteps: [{id: a, uses: test.import, with: {path: /x}}]\n")


def test_RUN_10_profile_vars_checked(h: Harness) -> None:
    with pytest.raises(RunValidationError, match="no default"):
        h.run("name: x\nvars: {inbox: {}}\nsteps: [{id: a, uses: test.echo}]\n")
    h.run(
        "name: x\nvars: {inbox: {}}\nsteps: [{id: a, uses: test.echo}]\n", run_vars={"inbox": "/x"}
    )


# -- RUN-11 provenance ---------------------------------------------------------


def test_RUN_11_run_records_workflow_hash_and_profile(h: Harness) -> None:
    import hashlib

    run = h.run(TWO)
    assert run.workflow_hash == hashlib.sha256(TWO.encode()).hexdigest()
    assert run.profile == "main"
    assert run.workflow == "two"
    assert h.engine.runs[run.id] is run


# -- RUN-13 ids ----------------------------------------------------------------


def test_RUN_13_ulids_are_26_char_crockford_and_time_sortable() -> None:
    from rifferr.core.ids import new_ulid

    a = new_ulid(now_ms=1_000, random=bytes(10))
    b = new_ulid(now_ms=1_001, random=b"\xff" * 10)
    c = new_ulid()
    for u in (a, b, c):
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", u)
    assert a < b
    assert a == "00000000Z8" + "0" * 16  # 1000 = 31*32 + 8
    assert new_ulid() != new_ulid()
