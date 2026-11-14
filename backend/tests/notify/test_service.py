"""S-NOTIFY rule notifications in the server (NOTIFY-7 to -10, -15), driven by events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppDB, AppLanes, AppNotifications, AppReviewGates
from rifferr.core.changeset import TargetRef
from rifferr.core.notify import Notification, NotifySettings, SendResult
from rifferr.core.profile import Profile, Secret
from rifferr.core.review import GateAlbum, ReviewGate
from rifferr.engine.events import Event, profile_stream
from rifferr.engine.lanes import Hold, Lane
from rifferr.notify.service import NotificationService

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Notification]] = []
        self.fail: set[str] = set()

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        self.sent.append((target, urls.reveal(), n))
        if target in self.fail:
            return SendResult(target=target, ok=False, error=f"down at {urls.reveal()}")
        return SendResult(target=target, ok=True)


class Secrets:
    def resolve(self, name: str) -> Secret:
        return Secret(f"fake://{name}-token")


class Env:
    def __init__(self, db: AppDB, notify: dict[str, Any]) -> None:
        self.db = db
        self.notifier = FakeNotifier()
        self.published: list[Event] = []
        self.now = T0
        self.settings = NotifySettings.model_validate({"retry": [], **notify})
        self.service = NotificationService(
            db,
            load_profile=self.profile,
            notifier=self.notifier,
            publish=self.published.append,
            secrets=lambda p: Secrets(),
            clock=lambda: self.now,
            flush_delay=60,
        )
        self.seq: dict[str, int] = {}

    def profile(self, name: str) -> Profile:
        return Profile(name=name, root=Path("/p"), notify=self.settings)

    def run(self, workflow: str = "inbox") -> str:
        return self.db.submit("main", {}, workflow=workflow).run

    def event(self, run_id: str, type: str, **data: Any) -> None:
        seq = self.seq.get(run_id, 0) + 1
        self.seq[run_id] = seq
        self.service.on_event(Event(seq=seq, ts=self.now, run_id=run_id, type=type, data=data))

    def waiting(self, run_id: str, key: str, label: str, *holds: tuple[str, str]) -> None:
        self.event(
            run_id,
            "lane.waiting",
            key=key,
            label=label,
            state="waiting",
            position="next",
            holds=[{"kind": k, "ref": r} for k, r in holds],
        )

    async def settle(self) -> None:
        await self.service.drain()

    def titles(self) -> list[tuple[str, str]]:
        return [(t, n.title) for t, _, n in self.notifier.sent]


@pytest.fixture
def db(tmp_path: Path) -> Iterator[AppDB]:
    d = AppDB(tmp_path / "app.db")
    yield d
    d.close()


@pytest.fixture
async def env(db: AppDB) -> AsyncIterator[Env]:
    e = Env(db, {"targets": {"phone": "ntfy"}, "base_url": "https://r.lan"})
    yield e
    await e.service.stop()


def lanes(db: AppDB, run_id: str, **states: int) -> None:
    out: list[Lane] = []
    for state, n in states.items():
        out += [Lane(f"{state}{i}", f"Album {state} {i}", state) for i in range(n)]  # type: ignore[arg-type]
    AppLanes(db).save(run_id, out)


# -- NOTIFY-8 review.needed ------------------------------------------------------


async def test_NOTIFY_8_one_review_needed_per_pass(env: Env) -> None:
    run = env.run()
    env.waiting(run, "pending:d1", "A - X", ("import", "d1"))
    env.waiting(run, "pending:d2", "B - Y", ("import", "d2"))
    env.event(run, "run.step.started", uses="next")  # the pass is over
    await env.settle()
    ((target, urls, n),) = env.notifier.sent
    assert target == "phone"
    assert urls == "fake://ntfy-token"
    assert n.title == "2 albums need review"
    assert "A - X" in n.body
    assert "B - Y" in n.body
    assert n.url == f"https://r.lan/review?run={run}"


async def test_NOTIFY_8_flushes_after_the_delay_without_another_event(db: AppDB) -> None:
    e = Env(db, {"targets": {"phone": "ntfy"}})
    e.service.flush_delay = 0.01
    run = e.run()
    e.waiting(run, "g", "A - X", ("gate", "g1"))
    await asyncio.sleep(0.05)
    await e.settle()
    assert e.titles() == [("phone", "1 album needs review")]
    await e.service.stop()


async def test_NOTIFY_8_summary_at_waiting_review_is_separate(env: Env) -> None:
    run = env.run()
    lanes(env.db, run, done=10, waiting=2)
    env.waiting(run, "a", "A - X", ("changeset", "cs1"))
    env.waiting(run, "b", "B - Y", ("changeset", "cs2"))
    env.event(run, "run.waiting_review", after="tag", changesets=["cs1", "cs2"], gates=[])
    await env.settle()
    titles = [n.title for _, _, n in env.notifier.sent]
    assert titles == ["2 albums need review", "inbox: 10 done, 2 waiting for review"]


async def test_NOTIFY_8_run_failed_and_summary(env: Env) -> None:
    run = env.run()
    lanes(env.db, run, failed=1)
    env.db.append_event(
        Event(
            seq=1,
            ts=T0,
            run_id=run,
            type="run.step.finished",
            step_id="tag",
            data={"status": "failed", "error": "boom\nmore", "failed_targets": []},
        )
    )
    env.event(run, "run.finished", status="failed")
    await env.settle()
    failed, summary = (n for _, _, n in env.notifier.sent)
    assert failed.type == "failure"
    assert "at step 'tag'" in failed.body
    assert "boom" in failed.body
    assert failed.url == f"https://r.lan/runs/{run}"
    assert summary.title == "inbox: 1 failed"


async def test_NOTIFY_8_empty_run_sends_no_summary_unless_asked(db: AppDB) -> None:
    e = Env(db, {"targets": {"phone": "ntfy"}})
    run = e.run()
    e.event(run, "run.finished", status="succeeded")
    await e.settle()
    assert e.notifier.sent == []
    e2 = Env(
        db, {"targets": {"phone": "ntfy"}, "rules": [{"on": "run.summary", "include_empty": True}]}
    )
    run2 = e2.run()
    e2.event(run2, "run.finished", status="succeeded")
    await e2.settle()
    assert [n.title for _, _, n in e2.notifier.sent] == ["inbox: no albums"]


async def test_NOTIFY_8_review_timeout_is_opt_in(db: AppDB) -> None:
    default = Env(db, {"targets": {"phone": "ntfy"}})
    run = default.run()
    default.event(run, "review.resolved", gate_id="g1", decision="reject", decided_by="timeout")
    await default.settle()
    assert default.notifier.sent == []
    opted = Env(db, {"targets": {"phone": "ntfy"}, "rules": [{"on": "review.timeout"}]})
    opted.event(run, "review.resolved", gate_id="g1", decision="reject", decided_by="timeout")
    opted.event(run, "review.resolved", gate_id="g2", decision="continue", decided_by="ui")
    await opted.settle()
    assert [n.title for _, _, n in opted.notifier.sent] == ["Decided by timeout: reject"]


async def test_NOTIFY_8_worker_problem_is_opt_in(db: AppDB) -> None:
    default = Env(db, {"targets": {"phone": "ntfy"}})
    default.service.on_worker_problem("main", "worker_crashed", "worker_crashed")
    await default.settle()
    assert default.notifier.sent == []
    opted = Env(db, {"targets": {"phone": "ntfy"}, "rules": [{"on": "worker.problem"}]})
    opted.service.on_worker_problem("main", "worker_failed_to_start", "no beetsdir")
    await opted.settle()
    ((_, _, n),) = opted.notifier.sent
    assert "could not start" in n.body
    assert "no beetsdir" in n.body
    (row,) = AppNotifications(db).for_profile("main")
    assert row.run_id is None


# -- NOTIFY-7 matching -----------------------------------------------------------


async def test_NOTIFY_7_rules_pick_targets_and_workflows(db: AppDB) -> None:
    e = Env(
        db,
        {
            "targets": {"phone": "ntfy", "discord": "discord"},
            "rules": [
                {"on": ["run.failed"], "targets": ["phone"]},
                {"on": "run.summary", "targets": ["discord"], "workflows": ["inbox"]},
            ],
        },
    )
    inbox, other = e.run("inbox"), e.run("other")
    lanes(db, inbox, done=1)
    lanes(db, other, done=1)
    e.event(inbox, "run.finished", status="succeeded")
    e.event(other, "run.finished", status="failed")
    await e.settle()
    assert sorted(e.titles()) == [("discord", "inbox: 1 done"), ("phone", "Run failed: other")]


async def test_NOTIFY_7_no_targets_no_rules(db: AppDB) -> None:
    e = Env(db, {})
    run = e.run()
    lanes(db, run, done=1)
    e.event(run, "run.finished", status="failed")
    await e.settle()
    assert e.notifier.sent == []


async def test_NOTIFY_7_empty_rules_turn_rule_notifications_off(db: AppDB) -> None:
    e = Env(db, {"targets": {"phone": "ntfy"}, "rules": []})
    run = e.run()
    lanes(db, run, done=1)
    e.event(run, "run.finished", status="failed")
    await e.settle()
    assert e.notifier.sent == []


# -- NOTIFY-4, -5, -9 recording and outcome events ----------------------------------


async def test_NOTIFY_4_outcome_on_the_profile_stream_without_the_url(env: Env) -> None:
    env.notifier.fail.add("phone")
    run = env.run()
    lanes(env.db, run, done=1)
    env.event(run, "run.finished", status="succeeded")
    await env.settle()
    (event,) = env.published
    assert event.run_id == profile_stream("main")
    assert event.type == "notify.failed"
    assert event.data["target"] == "phone"
    assert event.data["source"] == "rule"
    assert event.data["event"] == "run.summary"
    assert event.data["run_id"] == run
    assert "ntfy-token" not in str(event.data)
    (row,) = AppNotifications(env.db).for_run(run)
    assert row.state == "failed"
    assert row.error is not None
    assert "ntfy-token" not in row.error


async def test_NOTIFY_9_same_transition_is_sent_once(env: Env) -> None:
    run = env.run()
    lanes(env.db, run, done=1)
    finished = Event(seq=7, ts=T0, run_id=run, type="run.finished", data={"status": "succeeded"})
    env.service.on_event(finished)
    env.service.on_event(finished)  # e.g. seen again after a restart
    await env.settle()
    assert len(env.notifier.sent) == 1


async def test_NOTIFY_9_pending_rows_are_retried_at_start(db: AppDB) -> None:
    store = AppNotifications(db)
    store.add(
        profile="main",
        run_id=None,
        event="run.summary",
        dedup_key="k",
        target="phone",
        message=Notification(title="left over", body="b"),
    )
    e = Env(db, {"targets": {"phone": "ntfy"}})
    await e.service.start()
    await e.settle()
    await e.service.stop()
    assert e.titles() == [("phone", "left over")]
    (row,) = store.for_profile("main")
    assert row.state == "sent"
    assert row.attempts == 1


async def test_NOTIFY_9_a_profile_that_fails_to_load_is_skipped(db: AppDB) -> None:
    e = Env(db, {"targets": {"phone": "ntfy"}})

    def broken(name: str) -> Profile:
        raise ValueError("bad yaml")

    e.service._load_profile = broken  # pyright: ignore[reportPrivateUsage]
    run = e.run()
    e.event(run, "run.finished", status="failed")
    await e.settle()
    assert e.notifier.sent == []


async def test_NOTIFY_5_a_broken_rule_engine_never_raises(env: Env) -> None:
    env.service.on_event(
        Event(seq=1, ts=T0, run_id="no-such-run", type="run.finished", data={"status": "failed"})
    )
    await env.settle()
    assert env.notifier.sent == []


# -- NOTIFY-15 reminders ------------------------------------------------------------


def gate(db: AppDB, run_id: str, gate_id: str) -> None:
    AppReviewGates(db).add(
        ReviewGate(
            id=gate_id,
            profile="main",
            run_id=run_id,
            step_id="check",
            albums=[
                GateAlbum(
                    target=TargetRef(scope="album", id="a1"),
                    label="A - X",
                    message=None,
                    shown={},
                    changes=[],
                    conflicted=[],
                    imported_by_run=True,
                )
            ],
            created_at=T0,
        )
    )


async def test_NOTIFY_15_reminders_while_pending_up_to_the_limit(db: AppDB) -> None:
    e = Env(
        db,
        {
            "targets": {"phone": "ntfy"},
            "rules": [{"on": "review.needed", "remind_after": "1d", "remind_limit": 2}],
        },
    )
    run = e.run()
    gate(db, run, "g1")
    e.waiting(run, "a1", "A - X", ("gate", "g1"))
    e.service.flush_all()
    await e.settle()
    assert len(e.notifier.sent) == 1
    e.now = T0 + timedelta(hours=23)
    assert e.service.check_reminders() == 0
    e.now = T0 + timedelta(days=1)
    assert e.service.check_reminders() == 1
    await e.settle()
    assert e.notifier.sent[-1][2].title == "Reminder: 1 album still needs review"
    assert "waited 1 day" in e.notifier.sent[-1][2].body
    e.now = T0 + timedelta(days=2)
    assert e.service.check_reminders() == 1
    e.now = T0 + timedelta(days=3)
    assert e.service.check_reminders() == 0  # limit reached
    await e.settle()
    assert len(e.notifier.sent) == 3


async def test_NOTIFY_15_deciding_the_last_item_stops_reminders(db: AppDB) -> None:
    e = Env(
        db, {"targets": {"phone": "ntfy"}, "rules": [{"on": "review.needed", "remind_after": "1h"}]}
    )
    run = e.run()
    gate(db, run, "g1")
    e.waiting(run, "a1", "A - X", ("gate", "g1"))
    e.service.flush_all()
    await e.settle()
    AppReviewGates(db).decide("g1", "continued", by="ui", note=None)
    e.now = T0 + timedelta(hours=2)
    assert e.service.check_reminders() == 0
    assert AppNotifications(db).reminding() == []


async def test_NOTIFY_15_no_reminders_without_remind_after(env: Env) -> None:
    run = env.run()
    gate(env.db, run, "g1")
    env.waiting(run, "a1", "A - X", ("gate", "g1"))
    env.service.flush_all()
    await env.settle()
    env.now = T0 + timedelta(days=30)
    assert env.service.check_reminders() == 0


def test_hold_json_shape_matches_what_the_service_reads() -> None:
    assert Hold("gate", "g1").as_json() == {"kind": "gate", "ref": "g1"}
