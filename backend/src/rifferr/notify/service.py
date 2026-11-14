"""Rule notifications in the server (S-NOTIFY NOTIFY-7 to -10, -14, -15).

The service listens to every event the server persists (``WorkerSupervisor.subscribe``)
and to worker problems, turns matching ones into rule notifications, records each one
per target in the app DB *before* sending it (so a restart neither loses nor repeats
it), and sends it in the background. A failed send never touches the run (NOTIFY-5).

Rule notifications report their outcome on the profile's stream, not the run's: a
run's stream belongs to its worker while it runs, and a second writer there would make
the worker's next event collide (B-037).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import JsonValue

from rifferr.appdb import (
    AppChangeSets,
    AppDB,
    AppImportDecisions,
    AppLanes,
    AppNotifications,
    AppReviewGates,
    StoredNotification,
)
from rifferr.core import notify as messages
from rifferr.core.notify import Notification, NotifyRule, QueueRef, RuleEvent, SendResult
from rifferr.core.profile import Profile
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine.events import PROFILE_STREAM_PREFIX, Event, profile_stream
from rifferr.notify import Notifier, ProfileTargets, SecretSource, Sleep

log = logging.getLogger("rifferr.notify")

FLUSH_DELAY = 1.0
"""Seconds to wait for more ``lane.waiting`` events of the same pass (NOTIFY-8)."""
REMINDER_INTERVAL = 60.0
"""Longest wait between reminder checks (NOTIFY-15)."""
HOLDING_STATES = frozenset({"proposed", "conflicted"})

LoadProfile = Callable[[str], Profile]
Publish = Callable[[Event], None]
Clock = Callable[[], datetime]


def _default_secrets(profile: Profile) -> SecretSource:
    return ProfileSecrets(profile.secrets, SecretResolver())


@dataclass
class _Held:
    """Queue items a run's lanes started waiting for, collected until the pass ends."""

    items: dict[str, QueueRef] = field(default_factory=dict[str, QueueRef])
    timer: asyncio.TimerHandle | None = None


class NotificationService:
    def __init__(
        self,
        db: AppDB,
        *,
        load_profile: LoadProfile,
        notifier: Notifier,
        publish: Publish,
        secrets: Callable[[Profile], SecretSource] = _default_secrets,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
        flush_delay: float = FLUSH_DELAY,
        reminder_interval: float = REMINDER_INTERVAL,
    ) -> None:
        self.db = db
        self.store = AppNotifications(db)
        self._load_profile = load_profile
        self._notifier = notifier
        self._publish = publish
        self._secrets = secrets
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self.flush_delay = flush_delay
        self.reminder_interval = reminder_interval
        self._held: dict[str, _Held] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._reminder_task: asyncio.Task[None] | None = None

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Retry notifications a stopped server left pending, then watch for reminders."""
        for row in self.store.pending():
            self._spawn(row)
        if self._reminder_task is None:
            self._reminder_task = asyncio.create_task(self._reminder_loop())

    async def stop(self) -> None:
        if self._reminder_task is not None:
            self._reminder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reminder_task
            self._reminder_task = None
        for held in self._held.values():
            if held.timer is not None:
                held.timer.cancel()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()  # their rows stay pending and are retried at the next start
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def drain(self) -> None:
        """Wait for every send in flight (tests, and the test route)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # -- events --------------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Supervisor listener: called with each persisted event, in order."""
        try:
            self._on_event(event)
        except Exception:  # a notification problem must never break the server
            log.exception("notification rules failed for %s", event.type)

    def _on_event(self, event: Event) -> None:
        if event.run_id.startswith(PROFILE_STREAM_PREFIX):
            return
        run_id = event.run_id
        if event.type == "lane.waiting":
            self._collect(run_id, event.data)
            return
        if run_id in self._held and not event.type.startswith("lane."):
            self.flush(run_id)  # the pass is over: one notification for its holds
        if event.type == "run.waiting_review":
            self._summary(run_id, "waiting_review", event.seq)
        elif event.type == "run.finished":
            status = str(event.data.get("status", ""))
            if status == "failed":
                self._failed(run_id, event)
            self._summary(run_id, status, event.seq)
        elif event.type == "review.resolved" and event.data.get("decided_by") == "timeout":
            self._timeout(run_id, "gate", str(event.data.get("gate_id")), event)
        elif event.type == "import.resolved":
            decision = AppImportDecisions(self.db).get(str(event.data.get("decision_id")))
            if decision is not None and decision.decided_by == "timeout":
                self._timeout(run_id, "import", decision.id, event)

    def on_worker_problem(self, profile: str, reason: str, error: str | None) -> None:
        """Supervisor hook: a worker crashed, hung or could not start (NOTIFY-8)."""
        try:
            now = self._clock()
            self._dispatch(
                profile,
                None,
                None,
                "worker.problem",
                f"worker:{profile}:{reason}:{now.isoformat()}",
                lambda base: messages.worker_problem(
                    profile=profile, reason=reason, error=error, base_url=base
                ),
            )
        except Exception:
            log.exception("notification rules failed for a worker problem")

    # -- review.needed (NOTIFY-8) ---------------------------------------------

    def _collect(self, run_id: str, data: dict[str, JsonValue]) -> None:
        held = self._held.setdefault(run_id, _Held())
        label = str(data.get("label") or data.get("key") or "")
        holds = data.get("holds")
        for hold in holds if isinstance(holds, list) else []:
            if not isinstance(hold, dict):
                continue
            kind, ref = hold.get("kind"), hold.get("ref")
            if kind not in ("changeset", "gate", "import") or not isinstance(ref, str):
                continue
            old = held.items.get(ref)
            labels = (*old.labels, label) if old is not None else (label,)
            held.items[ref] = QueueRef(kind=kind, ref=ref, labels=tuple(dict.fromkeys(labels)))
        if held.timer is None:
            with contextlib.suppress(RuntimeError):  # no running loop: flushed by the next event
                loop = asyncio.get_running_loop()
                held.timer = loop.call_later(self.flush_delay, self.flush, run_id)

    def flush(self, run_id: str) -> None:
        held = self._held.pop(run_id, None)
        if held is None:
            return
        if held.timer is not None:
            held.timer.cancel()
        if not held.items:
            return
        items = list(held.items.values())
        job = self.db.job_for_run(run_id)
        if job is None:
            return
        refs = ",".join(sorted(held.items))
        self._dispatch(
            job.profile,
            run_id,
            job.workflow,
            "review.needed",
            f"{run_id}:review.needed:{refs}",
            lambda base: messages.review_needed(
                items, profile=job.profile, workflow=job.workflow, run_id=run_id, base_url=base
            ),
            items=items,
        )

    def flush_all(self) -> None:
        for run_id in list(self._held):
            self.flush(run_id)

    # -- run events ------------------------------------------------------------

    def _summary(self, run_id: str, status: str, seq: int) -> None:
        job = self.db.job_for_run(run_id)
        if job is None:
            return
        counts = AppLanes(self.db).counts([run_id]).get(run_id, {})
        self._dispatch(
            job.profile,
            run_id,
            job.workflow,
            "run.summary",
            f"{run_id}:run.summary:{seq}",
            lambda base: messages.run_summary(
                counts,
                status=status,
                profile=job.profile,
                workflow=job.workflow,
                run_id=run_id,
                base_url=base,
            ),
            empty=not counts,
        )

    def _failed(self, run_id: str, finished: Event) -> None:
        job = self.db.job_for_run(run_id)
        if job is None:
            return
        step, error = self._failure(run_id)
        if error is None:
            raw = finished.data.get("error") or finished.data.get("reason")
            error = str(raw) if raw else None
        self._dispatch(
            job.profile,
            run_id,
            job.workflow,
            "run.failed",
            f"{run_id}:run.failed:{finished.seq}",
            lambda base: messages.run_failed(
                profile=job.profile,
                workflow=job.workflow,
                run_id=run_id,
                step=step,
                error=error,
                base_url=base,
            ),
        )

    def _failure(self, run_id: str) -> tuple[str | None, str | None]:
        """The last failed step of a run and its first error, from the run's events."""
        for event in reversed(self.db.events_since(run_id, 0)):
            if event.type != "run.step.finished" or event.data.get("status") != "failed":
                continue
            error = event.data.get("error")
            targets = event.data.get("failed_targets")
            if not error and isinstance(targets, list) and targets:
                first = targets[0]
                error = first.get("message") if isinstance(first, dict) else None
            return event.step_id, str(error) if error else None
        return None, None

    def _timeout(self, run_id: str, what: str, ref: str, event: Event) -> None:
        job = self.db.job_for_run(run_id)
        if job is None:
            return
        decision = str(event.data.get("decision") or event.data.get("action") or "")
        self._dispatch(
            job.profile,
            run_id,
            job.workflow,
            "review.timeout",
            f"{run_id}:review.timeout:{ref}",
            lambda base: messages.review_timeout(
                what="gate" if what == "gate" else "import",
                ref=ref,
                decision=decision,
                profile=job.profile,
                workflow=job.workflow,
                run_id=run_id,
                base_url=base,
            ),
        )

    # -- matching and recording (NOTIFY-7, -9) ----------------------------------

    def _dispatch(
        self,
        profile_name: str,
        run_id: str | None,
        workflow: str | None,
        event: RuleEvent,
        dedup_key: str,
        build: Callable[[str | None], Notification],
        *,
        items: list[QueueRef] | None = None,
        empty: bool = False,
    ) -> None:
        profile = self._profile(profile_name)
        if profile is None:
            return
        settings = profile.notify
        chosen: dict[str, NotifyRule] = {}
        for rule in settings.effective_rules():
            if not rule.matches(event, workflow):
                continue
            if event == "run.summary" and empty and not rule.include_empty:
                continue
            for target in rule.targets or list(settings.targets):
                if target in settings.targets:
                    current = chosen.get(target)
                    if current is None or (current.remind_after is None and rule.remind_after):
                        chosen[target] = rule
        if not chosen:
            return
        message = build(settings.base_url)
        for target, rule in chosen.items():
            remind = rule.remind_after if event == "review.needed" else None
            row = self.store.add(
                profile=profile_name,
                run_id=run_id,
                event=event,
                dedup_key=dedup_key,
                target=target,
                message=message,
                items=items or [],
                remind_after=_seconds(remind) if remind else None,
                remind_limit=rule.remind_limit if remind else 0,
                now=self._clock(),
            )
            if row is not None:  # None: this transition was already handled
                self._spawn(row)

    def _profile(self, name: str) -> Profile | None:
        try:
            return self._load_profile(name)
        except Exception as exc:
            log.warning("notifications for profile %s skipped: %s", name, exc)
            return None

    # -- sending ---------------------------------------------------------------

    def _spawn(self, row: StoredNotification) -> None:
        task = asyncio.get_running_loop().create_task(self._send(row))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, row: StoredNotification) -> None:
        profile = self._profile(row.profile)
        if profile is None:
            result = SendResult(target=row.target, ok=False, error="profile could not be loaded")
        else:
            targets = ProfileTargets(
                profile.notify, self._secrets(profile), self._notifier, sleep=self._sleep
            )
            result = await targets.send(
                row.target, row.message, on_attempt=lambda n: self.store.attempt(row.id, n)
            )
        self.store.finish(row.id, result.ok, result.error, now=self._clock())
        data: dict[str, JsonValue] = {
            "target": row.target,
            "source": "rule",
            "event": row.event,
            "notification_id": row.id,
            "run_id": row.run_id,
        }
        if not result.ok:
            data["error"] = result.error
        self._emit(row.profile, "notify.sent" if result.ok else "notify.failed", data)

    def _emit(self, profile: str, type_: str, data: dict[str, JsonValue]) -> None:
        event = self.db.append_next(profile_stream(profile), type_, data)
        self._publish(event)

    async def send_test(self, profile: Profile, target: str) -> SendResult:
        """NOTIFY-14: one attempt, no record, no retries."""
        targets = ProfileTargets(
            profile.notify, self._secrets(profile), self._notifier, sleep=self._sleep
        )
        return await targets.send(
            target, messages.test_notification(profile.name, target), retry=False
        )

    # -- reminders (NOTIFY-15) ---------------------------------------------------

    async def _reminder_loop(self) -> None:
        while True:
            try:
                self.check_reminders()
            except Exception:
                log.exception("checking notification reminders failed")
            await asyncio.sleep(self.reminder_interval)

    def check_reminders(self) -> int:
        """Send due reminders; returns how many rows got one."""
        now = self._clock()
        sent = 0
        for row in self.store.reminding():
            if row.remind_after is None or row.run_id is None:
                self.store.stop_reminding(row.id)
                continue
            still = [i for i in row.items if self._still_pending(i)]
            if not still:
                self.store.stop_reminding(row.id)
                continue
            last = row.last_reminded_at or row.sent_at or row.created_at
            if now < last + timedelta(seconds=row.remind_after):
                continue
            profile = self._profile(row.profile)
            if profile is None:
                continue
            job = self.db.job_for_run(row.run_id)
            workflow = job.workflow if job is not None else None
            message = messages.review_needed(
                still,
                profile=row.profile,
                workflow=workflow,
                run_id=row.run_id,
                base_url=profile.notify.base_url,
                waited=now - row.created_at,
            )
            reminder = self.store.add(
                profile=row.profile,
                run_id=row.run_id,
                event="review.needed",
                dedup_key=f"{row.dedup_key}:remind:{row.reminders + 1}",
                target=row.target,
                message=message,
                items=still,
                parent=row.id,
                now=now,
            )
            self.store.reminded(row.id, now)
            if reminder is not None:
                self._spawn(reminder)
                sent += 1
        return sent

    def _still_pending(self, item: QueueRef) -> bool:
        if item.kind == "gate":
            gate = AppReviewGates(self.db).get(item.ref)
            return gate is not None and gate.state == "pending"
        if item.kind == "import":
            decision = AppImportDecisions(self.db).get(item.ref)
            return decision is not None and decision.state == "pending"
        stored = AppChangeSets(self.db).get(item.ref)
        return stored is not None and stored.state in HOLDING_STATES


def _seconds(duration: str) -> int:
    from rifferr.core.duration import parse_duration

    return int(parse_duration(duration).total_seconds())
