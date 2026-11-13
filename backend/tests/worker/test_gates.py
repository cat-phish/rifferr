"""S-REVIEW decisions: decide once, continue, reject, cancel, timeouts, restarts
(REVIEW-6 .. -12, RUN-14)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppChangeSets, AppDB, AppReviewGates, GateResolvedError, SqliteEventSink
from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.review import ReviewGate
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import config_root, create_profile, load_profile
from rifferr.steps.review import ReviewStep
from rifferr.worker.process import Commands, Worker
from rifferr.worker.protocol import WorkerCommand, WorkerMessage
from rifferr.worker.supervisor import WorkerSupervisor
from tests.engine.steps import ALL
from tests.worker.conftest import wf

RUN = "01RUNGATE00000000000000001"
GATE = "{id: check, uses: review, with: {message: 'Check $album', show: [label]}}"
AFTER = "{id: after, uses: test.echo, with: {message: 'after: {{ steps.check.decision }}'}}"
TAG = "{id: tag, uses: test.propose, apply: auto, with: {set: {label: L}}}"
TAG_GATE_AFTER = wf(TAG, GATE, AFTER)


def gate_with(**params: str) -> str:
    inner = ", ".join(f"{k}: {v}" for k, v in params.items())
    return f"{{id: check, uses: review, with: {{{inner}}}}}"


class Harness:
    """An in-process worker plus a supervisor on the same app DB. The supervisor's
    wake-ups go to the in-process worker instead of spawning one."""

    def __init__(self, tmp_path: Path, backend: FakeBackend | None = None) -> None:
        if not (config_root() / "profiles" / "main").exists():
            create_profile(config_root(), "main")
        self.tmp_path = tmp_path
        self.db = AppDB(tmp_path / "app.db")
        self.sink = SqliteEventSink(self.db)
        self.queue: Commands = asyncio.Queue()
        self.backend = backend or FakeBackend()
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep()]),
            self.backend,
            self._send,
        )
        self.gates = AppReviewGates(self.db)
        self.changesets = AppChangeSets(self.db)
        self.now = datetime.now(UTC)
        self.supervisor = self.make_supervisor(self.db)

    def make_supervisor(self, db: AppDB) -> WorkerSupervisor:
        supervisor = WorkerSupervisor(
            db, SqliteEventSink(db), command=lambda p: ["false"], clock=lambda: self.now
        )

        async def wake(profile: str) -> None:
            self.queue.put_nowait(WorkerCommand(kind="start_run", profile=profile))

        supervisor._wake = wake  # type: ignore[method-assign]
        return supervisor

    def _send(self, message: WorkerMessage) -> None:
        if message.event is not None:
            self.sink.append(message.event)

    def album(self, name: str, **fields: Any) -> None:
        self.backend.add_album(
            {"album": name, "year": 2001, RUN_MARKER: RUN, **fields, "items": [{"title": "t"}]}
        )

    def submit(self, source: str) -> None:
        payload: dict[str, Any] = {"source": source, "source_name": "wf.yaml", "vars": {}}
        self.db.submit("main", payload, run_id=RUN)
        self.queue.put_nowait(WorkerCommand(kind="start_run", profile="main"))

    def events(self, type: str | None = None) -> list[Any]:
        return [e for e in self.db.events_since(RUN, 0) if type is None or e.type == type]

    def types(self) -> list[str]:
        return [e.type for e in self.events()]

    def logs(self) -> list[str]:
        return [str(e.data.get("line")) for e in self.events("run.step.log")]

    def state(self) -> str:
        job = self.db.job_for_run(RUN)
        assert job is not None
        return job.state

    def gate(self) -> ReviewGate:
        (gate,) = self.gates.for_run(RUN)
        return gate

    def labels(self) -> dict[str, Any]:
        return {
            str(a.fields["album"]): a.fields.get("label") for a in self.backend.query(Selection())
        }

    async def until(self, check: Any) -> None:
        async def poll() -> None:
            while not check():
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 5)

    async def waiting(self, source: str = TAG_GATE_AFTER, *albums: str) -> ReviewGate:
        for name in albums or ("A",):
            self.album(name)
        self.submit(source)
        await self.until(lambda: self.state() == "waiting_review")
        return self.gate()


@contextlib.asynccontextmanager
async def serving(harness: Harness) -> AsyncIterator[Harness]:
    task = asyncio.create_task(harness.worker.serve(harness.queue))
    try:
        yield harness
    finally:
        harness.queue.put_nowait(None)
        await asyncio.wait_for(task, 5)


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[Harness]:
    harness = Harness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


# -- RUN-14 / REVIEW-3: the suspended run ------------------------------------------------


async def test_REVIEW_3_gate_is_stored_and_the_run_waits_without_a_worker(h: Harness) -> None:
    gate = await h.waiting()
    assert gate.state == "pending"
    (album,) = gate.albums
    assert (album.message, album.shown, album.imported_by_run) == ("Check A", {"label": "L"}, True)
    assert [(c.field, c.before, c.after) for c in album.changes if c.target.scope == "album"] == [
        ("label", None, "L")
    ]
    job = h.db.job_for_run(RUN)
    assert job is not None
    assert (job.payload["resume_after"], job.payload["gates"], job.payload["holds"]) == (
        "check",
        [gate.id],
        [],
    )
    assert [j.state for j in h.db.jobs("main")] == ["waiting_review"]
    assert h.events("review.requested")[0].data == {"gate_id": gate.id, "albums": 1}
    assert h.types()[-1] == "run.waiting_review"


# -- REVIEW-6: decide once ------------------------------------------------------------------


async def test_REVIEW_6_a_gate_is_decided_exactly_once(h: Harness) -> None:
    gate = await h.waiting()
    await h.supervisor.decide_gate(gate.id, "continue", by="ui", note="looks fine")
    with pytest.raises(GateResolvedError, match="already continued"):
        await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    with pytest.raises(KeyError):
        await h.supervisor.decide_gate("nope", "continue")
    decided = h.gates.get(gate.id)
    assert decided is not None
    assert (decided.state, decided.decided_by, decided.note) == ("continued", "ui", "looks fine")
    assert decided.decided_at is not None
    (resolved,) = h.events("review.resolved")
    assert resolved.step_id == "check"
    assert resolved.data["gate_id"] == gate.id
    assert (resolved.data["decision"], resolved.data["decided_by"]) == ("continue", "ui")


async def test_REVIEW_6_gate_content_is_fixed_at_creation(h: Harness) -> None:
    gate = await h.waiting()
    h.backend.add_album({"album": "Late", RUN_MARKER: RUN, "items": [{"title": "t"}]})
    again = h.gates.get(gate.id)
    assert again is not None
    assert again.albums == gate.albums


async def test_REVIEW_6_store_decides_once_even_when_racing(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    async with serving(harness):
        gate = await harness.waiting()
        other = AppDB(tmp_path / "app.db")  # a second connection, like a second request
        try:
            AppReviewGates(other).decide(gate.id, "continued", by="one")
            with pytest.raises(GateResolvedError):
                harness.gates.decide(gate.id, "rejected", by="two")
        finally:
            other.close()
    harness.db.close()


async def test_REVIEW_6_decisions_wait_until_the_run_is_suspended(h: Harness) -> None:
    gate = ReviewGate(
        id="01GATEEARLY000000000000001",
        profile="main",
        run_id=RUN,
        step_id="check",
        albums=[],
        created_at=h.now,
    )
    h.db.submit("main", {"source": "", "vars": {}}, run_id=RUN)  # queued, never claimed
    h.gates.add(gate)
    with pytest.raises(ValueError, match="queued"):
        await h.supervisor.decide_gate(gate.id, "continue")
    still = h.gates.get(gate.id)
    assert still is not None
    assert still.state == "pending"


# -- REVIEW-7: continue -------------------------------------------------------------------


async def test_REVIEW_7_continue_resumes_at_the_next_step(h: Harness) -> None:
    gate = await h.waiting()
    jobs_before = len(h.db.jobs("main"))
    await h.supervisor.decide_gate(gate.id, "continue", by="ui", note="ok")
    assert len(h.db.jobs("main")) == jobs_before  # no worker job for continue (REVIEW-12)
    await h.until(lambda: h.state() == "succeeded")
    assert "after: continue" in h.logs()
    types = h.types()
    assert types.index("review.resolved") < types.index("run.resumed")
    assert types[-1] == "run.finished"
    seqs = [e.seq for e in h.events()]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_REVIEW_7_step_record_shows_the_decision(h: Harness) -> None:
    from rifferr.worker.process import step_records

    gate = await h.waiting()
    await h.supervisor.decide_gate(gate.id, "continue", by="ui", note="ok")
    await h.until(lambda: h.state() == "succeeded")
    record = step_records(h.events())["check"]
    assert record.outputs["gate"] == gate.id
    assert (record.outputs["decision"], record.outputs["decided_by"], record.outputs["note"]) == (
        "continue",
        "ui",
        "ok",
    )
    assert isinstance(record.outputs["decided_at"], str)


def test_RUN_14_run_resumes_only_when_changesets_and_gates_are_all_released(
    tmp_path: Path,
) -> None:
    from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef

    create_profile(config_root(), "main")
    db = AppDB(tmp_path / "app.db")
    try:
        db.submit("main", {"source": "", "vars": {}}, run_id=RUN)
        assert db.claim_next("main") is not None
        cs = ChangeSet(
            id="01CSHOLD000000000000000001",
            profile="main",
            run_id=RUN,
            source=ChangeSource(kind="step", name="test"),
            created_at=datetime.now(UTC),
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id="a1"),
                    changes=[FieldChange(field="genre", new="Pop")],
                )
            ],
        )
        AppChangeSets(db).add(cs)
        gates = AppReviewGates(db)
        gate = ReviewGate(
            id="01GATEHOLD0000000000000001",
            profile="main",
            run_id=RUN,
            step_id="s",
            albums=[],
            created_at=datetime.now(UTC),
        )
        gates.add(gate)
        assert db.suspend(RUN, "s", [cs.id], [gate.id])
        gates.decide(gate.id, "continued", by="ui")
        assert not db.requeue_if_released(RUN)  # the ChangeSet still holds it
        AppChangeSets(db).set_state(cs.id, "rejected")
        assert db.requeue_if_released(RUN)
        job = db.job_for_run(RUN)
        assert job is not None
        assert job.state == "queued"
    finally:
        db.close()


def test_RUN_14_a_rejected_gate_never_releases_the_run(tmp_path: Path) -> None:
    create_profile(config_root(), "main")
    db = AppDB(tmp_path / "app.db")
    try:
        db.submit("main", {"source": "", "vars": {}}, run_id=RUN)
        assert db.claim_next("main") is not None
        gates = AppReviewGates(db)
        gate = ReviewGate(
            id="01GATEHOLD0000000000000002",
            profile="main",
            run_id=RUN,
            step_id="s",
            albums=[],
            created_at=datetime.now(UTC),
        )
        gates.add(gate)
        assert db.suspend(RUN, "s", [], [gate.id])
        gates.decide(gate.id, "rejected", by="ui")
        assert not db.requeue_if_released(RUN)
    finally:
        db.close()


# -- REVIEW-8: reject ----------------------------------------------------------------------


async def test_REVIEW_8_reject_with_stop_cancels_the_run(h: Harness) -> None:
    gate = await h.waiting(wf(GATE, AFTER))
    # A ChangeSet the run proposed that nobody decided.
    from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef

    album_id = gate.albums[0].target.id
    pending = ChangeSet(
        id="01CSPENDING000000000000001",
        profile="main",
        run_id=RUN,
        step_id="check",
        source=ChangeSource(kind="step", name="test"),
        created_at=h.now,
        targets=[
            TargetChange(
                target=TargetRef(scope="album", id=album_id),
                changes=[FieldChange(field="genre", new="Pop")],
            )
        ],
    )
    h.changesets.add(pending)
    await h.supervisor.decide_gate(gate.id, "reject", by="ui", note="wrong album")
    assert h.state() == "cancelled"
    finished = h.events("run.finished")[-1]
    assert finished.data == {"status": "cancelled", "reason": "rejected_at_review"}
    job = h.db.job_for_run(RUN)
    assert job is not None
    assert job.error == "rejected_at_review"
    stored = h.changesets.get(pending.id)
    assert stored is not None
    assert (stored.state, stored.resolved_by) == ("rejected", "review")
    assert "after: " not in " ".join(h.logs())  # later steps never ran
    assert h.labels() == {"A": None}
    rejected = h.gates.get(gate.id)
    assert rejected is not None
    assert rejected.state == "rejected"


async def test_REVIEW_8_reject_with_stop_keeps_applied_changes(h: Harness) -> None:
    gate = await h.waiting()
    await h.supervisor.decide_gate(gate.id, "reject")
    assert h.state() == "cancelled"
    assert h.labels() == {"A": "L"}


async def test_REVIEW_8_reject_with_undo_reverts_the_runs_changes(h: Harness) -> None:
    source = wf(
        TAG,
        "{id: year, uses: test.propose, apply: auto, with: {set: {year: 2002}}}",
        gate_with(on_reject="undo"),
        AFTER,
    )
    gate = await h.waiting(source)
    assert h.labels() == {"A": "L"}
    await h.supervisor.decide_gate(gate.id, "reject", by="ui")
    await h.until(lambda: h.state() == "cancelled")
    await h.until(lambda: "run.finished" in h.types())
    assert h.labels() == {"A": None}
    assert h.backend.query(Selection())[0].fields["year"] == 2001
    lines = h.logs()
    assert sum("rejected: undid journal entry" in line for line in lines) == 2
    assert any("1 album(s) this run imported stay in the library" in line for line in lines)
    undone = [e.data["changeset_id"] for e in h.events("changeset.undone")]
    originals = [
        s.changeset.id for s in h.changesets.for_run(RUN) if s.changeset.source.kind == "step"
    ]
    assert undone == list(reversed(originals))  # newest first (JOURNAL-6)
    assert h.events("run.finished")[-1].data == {
        "status": "cancelled",
        "reason": "rejected_at_review",
    }
    (reject_job,) = [j for j in h.db.jobs("main") if j.kind == "review_reject"]
    assert reject_job.state == "succeeded"


async def test_REVIEW_8_undo_conflict_is_logged_and_the_run_still_ends(h: Harness) -> None:
    from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef

    gate = await h.waiting(wf(TAG, gate_with(on_reject="undo")))
    album_id = gate.albums[0].target.id
    h.backend.apply(  # someone edits the label while the gate waits
        ChangeSet(
            id="manual",
            profile="main",
            source=ChangeSource(kind="manual", name="test"),
            created_at=h.now,
            targets=[
                TargetChange(
                    target=TargetRef(scope="album", id=album_id),
                    changes=[FieldChange(field="label", old="L", new="Mine")],
                )
            ],
        )
    )
    await h.supervisor.decide_gate(gate.id, "reject")
    await h.until(lambda: "run.finished" in h.types())
    assert h.state() == "cancelled"
    assert h.labels() == {"A": "Mine"}
    errors = [str(e.data["line"]) for e in h.events("run.step.log") if e.data["stream"] == "error"]
    assert any("undo stopped at journal entry" in line and "label" in line for line in errors)
    (reject_job,) = [j for j in h.db.jobs("main") if j.kind == "review_reject"]
    assert reject_job.state == "failed"


async def test_REVIEW_8_undo_with_nothing_applied_says_so(h: Harness) -> None:
    gate = await h.waiting(wf(gate_with(on_reject="undo")))
    await h.supervisor.decide_gate(gate.id, "reject")
    await h.until(lambda: "run.finished" in h.types())
    assert any("no applied tag changes to undo" in line for line in h.logs())
    assert h.state() == "cancelled"


# -- REVIEW-9: timeouts ---------------------------------------------------------------------


async def test_REVIEW_9_overdue_gate_is_decided_by_timeout(h: Harness) -> None:
    gate = await h.waiting(wf(gate_with(timeout="1h", on_timeout="continue"), AFTER))
    assert await h.supervisor.decide_overdue_gates() == []  # not due yet
    h.now = h.now + timedelta(hours=2)
    assert await h.supervisor.decide_overdue_gates() == [gate.id]
    await h.until(lambda: h.state() == "succeeded")
    decided = h.gates.get(gate.id)
    assert decided is not None
    assert (decided.state, decided.decided_by) == ("continued", "timeout")
    assert "after: continue" in h.logs()


async def test_REVIEW_9_timeout_defaults_to_reject(h: Harness) -> None:
    gate = await h.waiting(wf(gate_with(timeout="30m"), AFTER))
    h.now = h.now + timedelta(minutes=31)
    await h.supervisor.decide_overdue_gates()
    assert h.state() == "cancelled"
    decided = h.gates.get(gate.id)
    assert decided is not None
    assert (decided.state, decided.decided_by) == ("rejected", "timeout")


async def test_REVIEW_9_overdue_gate_is_decided_at_server_start(h: Harness) -> None:
    gate = await h.waiting(wf(gate_with(timeout="1h", on_timeout="continue"), AFTER))
    h.now = h.now + timedelta(days=1)
    restarted = h.make_supervisor(AppDB(h.tmp_path / "app.db"))  # a new server process
    restarted.gate_check_interval = 3600
    restarted.start()
    try:
        await h.until(lambda: h.state() == "succeeded")
    finally:
        await restarted.shutdown()
        restarted.db.close()
    decided = h.gates.get(gate.id)
    assert decided is not None
    assert decided.decided_by == "timeout"


async def test_REVIEW_9_timer_wakes_for_a_new_gate(h: Harness) -> None:
    h.supervisor.gate_check_interval = 3600
    h.supervisor.start()
    try:
        # The deadline is in the past by the supervisor's clock as soon as it exists.
        h.now = h.now + timedelta(days=1)
        h.album("A")
        h.submit(wf(gate_with(timeout="1h", on_timeout="continue"), AFTER))
        await h.until(lambda: h.state() == "waiting_review")
        # The worker's events reach this supervisor only through its sink here, so poke
        # it the way a forwarded review.requested event would.
        h.supervisor._persist(  # pyright: ignore[reportPrivateUsage]
            h.events("review.requested")[0].model_copy(update={"seq": h.db.last_seq(RUN) + 1})
        )
        await h.until(lambda: h.state() == "succeeded")
    finally:
        await h.supervisor.shutdown()


# -- REVIEW-10: cancel while waiting ----------------------------------------------------------


async def test_REVIEW_10_cancelling_a_waiting_run_cancels_its_gate(h: Harness) -> None:
    gate = await h.waiting()
    await h.supervisor.cancel(RUN)
    assert h.state() == "cancelled"
    cancelled = h.gates.get(gate.id)
    assert cancelled is not None
    assert cancelled.state == "cancelled"
    with pytest.raises(GateResolvedError, match="cancelled"):
        await h.supervisor.decide_gate(gate.id, "continue")
    assert h.events("run.finished")[-1].data == {"status": "cancelled"}


# -- REVIEW-11: restarts ----------------------------------------------------------------------


async def test_REVIEW_11_pending_gate_survives_server_and_worker_restarts(
    tmp_path: Path,
) -> None:
    first = Harness(tmp_path)
    async with serving(first):
        gate = await first.waiting()
    first.db.close()  # server and worker gone

    second = Harness(tmp_path, backend=first.backend)  # same library and app DB
    async with serving(second):
        assert second.state() == "waiting_review"
        assert second.gate() == gate
        await second.supervisor.decide_gate(gate.id, "continue", by="cli")
        await second.until(lambda: second.state() == "succeeded")
        assert "after: continue" in second.logs()
    second.db.close()
