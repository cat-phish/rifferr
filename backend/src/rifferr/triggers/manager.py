"""The trigger manager (F-TRIGGERS): arms workflows' ``on:`` blocks in the server.

Triggers live in the API server, not in a worker: workers stop when idle, and a
trigger only has to submit a run through ``WorkerSupervisor.submit_run``.

- A reconcile loop re-reads every profile's workflow files and their trust and re-arms
  what changed (TRIGGERS-1, -2). It also retries watch paths that were missing (-3).
- Each armed watch runs a ``watchfiles`` task with a :class:`SettleTracker` (-8 .. -11).
- Each armed schedule runs a task that sleeps until the next cron time (-12 .. -14).
- Webhooks are a lookup table used by the ``/hooks`` route (-15 .. -18).
- Every submission goes through :meth:`TriggerManager.fire`, which deduplicates (-19 ..
  -22) and emits ``trigger.*`` events (-24).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import JsonValue

from rifferr.appdb import AppDB, AppTriggers
from rifferr.core.profile import Profile, ProfileError, ProfileLoadError, merged_vars
from rifferr.core.triggers import (
    ACTIVE_STATES,
    InboxState,
    SettleTracker,
    SkipReason,
    dedup_skip,
    inbox_state,
    parse_duration,
    top_level,
)
from rifferr.core.workflow import (
    CALL_TYPE,
    WORKFLOW_SUFFIXES,
    Calls,
    InterpolationContext,
    InterpolationError,
    Workflow,
    WorkflowLoadError,
    interpolate,
    load_workflow,
    validate,
)
from rifferr.core.workflow.registry import StepRegistry
from rifferr.engine.events import Event, profile_stream
from rifferr.profile_store import (
    WORKFLOWS_DIR,
    effective_timezone,
    list_profiles,
    load_profile,
    profile_dir,
)
from rifferr.triggers import fs
from rifferr.trust import WorkflowFileError, read_workflow_file, workflow_hash
from rifferr.workflow_calls import called_trust, read_calls

log = logging.getLogger(__name__)

Kind = Literal["watch", "schedule", "webhook"]
FireKind = Literal["manual", "watch", "schedule", "webhook"]
StateName = Literal["armed", "disabled", "error"]

FORCE_POLLING_ENV = "RIFFERR_FORCE_POLLING"
PATH_RETRY = 60.0
WATCH_TICK_MS = 500


def _calls_digest(sha: str, calls: Calls) -> str:
    """One digest over the workflow and every file it calls, so any change re-arms."""
    parts = [sha]
    for path, f in sorted(calls.files.items()):
        parts.append(f"{path}={workflow_hash(f.text) if f.text else 'unreadable'}")
    parts.extend(str(e) for e in calls.errors)
    return "calls:" + workflow_hash("\n".join(parts))


class Submit(Protocol):
    async def __call__(
        self,
        profile: str,
        source: str,
        *,
        source_name: str,
        workflow: str | None = None,
        trigger: str = "manual",
        run_input: str | None = None,
        called: Mapping[str, str] | None = None,
    ) -> str: ...


@dataclass
class TriggerInfo:
    """One trigger's state for ``GET /api/profiles/{profile}/triggers`` (TRIGGERS-4)."""

    workflow: str
    kind: Kind
    state: StateName
    detail: str | None = None
    path: str | None = None
    next_fire: datetime | None = None


@dataclass
class Armed:
    """A workflow file of a profile and what is armed for it."""

    profile: str
    name: str
    path: Path
    sha256: str
    """The digest that decides re-arming: the file's hash, plus every called file's when
    it calls any (SEC-61)."""
    text: str = ""
    calls: Calls | None = None
    """The files it calls, read with it; a fired run gets exactly these texts (SEC-61)."""
    workflow: Workflow | None = None
    error: str | None = None
    trusted: bool = False
    watch_path: Path | None = None
    within: Path | None = None
    input: str = ""
    timezone: tzinfo = UTC
    triggers: dict[Kind, TriggerInfo] = field(default_factory=dict[Kind, TriggerInfo])
    tasks: list[asyncio.Task[None]] = field(default_factory=list[asyncio.Task[None]])
    retry_at: float | None = None
    """Monotonic time to re-arm at (a missing watch path, TRIGGERS-3)."""
    tracker: SettleTracker | None = None
    """The watch trigger's settling candidates (the inbox, F-DASHBOARD DASHBOARD-1)."""


#: Inputs listed per watched folder (DASHBOARD-2).
INBOX_LIMIT = 200


def _list_inputs(root: Path, limit: int) -> tuple[list[tuple[str, bool, str | None]], int] | None:
    """``(name, inside the folder, fingerprint)`` of the first ``limit`` candidate inputs
    and how many were left out; ``None`` if the folder can't be read. A symbolic link out
    of the folder is not followed (DASHBOARD-2)."""
    try:
        real_root = root.resolve(strict=True)
        names = sorted(e.name for e in root.iterdir() if e.is_dir())
    except OSError:
        return None
    entries: list[tuple[str, bool, str | None]] = []
    for name in names[:limit]:
        path = root / name
        if not path.resolve().is_relative_to(real_root):
            entries.append((name, False, None))
        else:
            entries.append((name, True, fs.input_fingerprint(path)))
    return entries, max(0, len(names) - limit)


@dataclass(frozen=True)
class InboxItem:
    path: str
    state: InboxState
    changed_at: datetime | None
    run_id: str | None


@dataclass
class InboxFolder:
    workflow: str
    folder: str
    trigger_state: StateName
    detail: str | None
    entries: list[InboxItem] = field(default_factory=list[InboxItem])
    omitted: int = 0


@dataclass(frozen=True)
class FireResult:
    run_id: str | None
    skipped: SkipReason | None = None


class WebhookError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class TriggerManager:
    def __init__(
        self,
        *,
        config_root: Path,
        db: AppDB,
        registry: StepRegistry,
        submit: Submit,
        publish: Callable[[Event], None] = lambda _e: None,
        reconcile_interval: float = 2.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config_root = config_root
        self.db = db
        self.store = AppTriggers(db)
        self.registry = registry
        self.submit = submit
        self.publish = publish
        self.reconcile_interval = reconcile_interval
        self.now = now
        self.monotonic = monotonic or asyncio.get_running_loop().time
        self.sleep = sleep
        self.env = os.environ if env is None else env
        self.armed: dict[tuple[str, str], Armed] = {}
        self._loop_task: asyncio.Task[None] | None = None
        self._fire_lock = asyncio.Lock()
        self._stopping = False
        self._parsed: dict[tuple[str, str], tuple[str, Workflow | None]] = {}

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        await self.reconcile()
        self._loop_task = asyncio.create_task(self._reconcile_loop(), name="triggers")

    async def stop(self) -> None:
        """TRIGGERS-25: stop watchers, schedules and webhooks; drop settle timers."""
        self._stopping = True
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        for armed in list(self.armed.values()):
            await self._disarm(armed)
        self.armed.clear()

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(self.reconcile_interval)
            try:
                await self.reconcile()
            except Exception:  # never let one bad profile stop triggers for good
                log.exception("trigger reconcile failed")

    # -- arming ------------------------------------------------------------------------

    def _workflow_files(self, profile: str) -> dict[str, list[Path]]:
        folder = profile_dir(self.config_root, profile) / WORKFLOWS_DIR
        files: dict[str, list[Path]] = {}
        if folder.is_dir():
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix in WORKFLOW_SUFFIXES:
                    files.setdefault(f.stem, []).append(f)
        return files

    async def reconcile(self) -> None:
        """Re-arm every workflow whose file, trust or retry time changed (TRIGGERS-1)."""
        if self._stopping:
            return
        seen: set[tuple[str, str]] = set()
        for profile_name in list_profiles(self.config_root):
            for name, paths in self._workflow_files(profile_name).items():
                key = (profile_name, name)
                seen.add(key)
                path = paths[0]
                try:
                    text, sha = read_workflow_file(path)
                except (OSError, WorkflowFileError) as exc:
                    text, sha = "", f"error:{exc}"
                ambiguous = len(paths) > 1
                if ambiguous:
                    sha = "ambiguous:" + ",".join(p.name for p in paths)
                calls = None if ambiguous or not text else self._read_calls(key, path, sha, text)
                trusted = (  # SEC-61, SUBWORKFLOW-13: the file and every file it calls
                    not ambiguous
                    and self.db.is_trusted(sha, profile_name)
                    and all(c.trusted for c in called_trust(self.db, calls, profile_name))
                )
                if calls is not None:
                    sha = _calls_digest(sha, calls)
                old = self.armed.get(key)
                retry_at = old.retry_at if old is not None else None
                due = retry_at is not None and self.monotonic() >= retry_at
                if old and old.sha256 == sha and old.trusted == trusted and not due:
                    continue
                if old is not None:
                    await self._disarm(old)
                armed = Armed(profile_name, name, path, sha, text, calls, trusted=trusted)
                if ambiguous:
                    armed.error = f"'{name}' matches {', '.join(p.name for p in paths)}"
                self.armed[key] = armed
                await self._arm(armed)
        for key in [k for k in self.armed if k not in seen]:
            await self._disarm(self.armed.pop(key))
        for key in [k for k in self._parsed if k not in seen]:
            del self._parsed[key]

    def _read_calls(self, key: tuple[str, str], path: Path, sha: str, text: str) -> Calls | None:
        """The files the workflow calls, read now (SEC-61); ``None`` when it calls none.
        The workflow is parsed once per content."""
        cached = self._parsed.get(key)
        if cached is not None and cached[0] == sha:
            wf = cached[1]
        else:
            try:
                wf, _ = load_workflow(text, str(path))
            except WorkflowLoadError:
                wf = None  # _arm reports it
            self._parsed[key] = (sha, wf)
        if wf is None or not any(s.uses == CALL_TYPE for s in wf.steps):
            return None
        folder = profile_dir(self.config_root, key[0]) / WORKFLOWS_DIR
        return read_calls(folder, wf, None, path)

    async def _arm(self, armed: Armed) -> None:
        if armed.error is None and not armed.text:
            armed.error = "workflow file could not be read"
        wf: Workflow | None = None
        profile: Profile | None = None
        if armed.error is None:
            try:
                profile = load_profile(self.config_root, armed.profile, self.env)
                wf, doc = load_workflow(armed.text, str(armed.path))
                errors = validate(wf, self.registry, None, profile.vars, doc=doc, calls=armed.calls)
                if errors:
                    armed.error = "; ".join(str(e) for e in errors[:3])
            except WorkflowLoadError as exc:
                armed.error = "; ".join(str(e) for e in exc.errors[:3])
            except (ProfileError, ProfileLoadError) as exc:
                armed.error = f"profile: {exc}"
        if wf is None or wf.on is None:
            if armed.error is not None:
                self._emit_error(armed, None, armed.error)
            return  # nothing to arm (no on: block, or unreadable)
        armed.workflow = wf
        present: dict[Kind, object] = {
            "watch": wf.on.watch,
            "schedule": wf.on.schedule,
            "webhook": wf.on.webhook,
        }
        kinds: list[Kind] = [k for k, v in present.items() if v is not None]
        if armed.error is not None or profile is None:
            for k in kinds:
                armed.triggers[k] = TriggerInfo(armed.name, k, "error", armed.error)
            self._emit_error(armed, None, armed.error or "invalid workflow")
            return
        if not armed.trusted:  # TRIGGERS-2
            for k in kinds:
                armed.triggers[k] = TriggerInfo(armed.name, k, "disabled", "untrusted")
            return
        try:
            ctx = InterpolationContext(
                vars=merged_vars(profile.vars, wf.vars), profile={"name": profile.name}
            )
            armed.input = interpolate(wf.input, ctx) if wf.input else ""
            if wf.on.watch is not None:
                armed.watch_path = Path(interpolate(wf.on.watch.path, ctx))
            if wf.on.webhook is not None:
                within = wf.on.webhook.within
                armed.within = Path(interpolate(within, ctx)) if within else armed.watch_path
                if armed.within is not None and not armed.within.is_absolute():  # SEC-85
                    raise InterpolationError(
                        f"webhook 'within' must be an absolute path, got '{armed.within}'"
                    )
        except InterpolationError as exc:
            for k in kinds:
                armed.triggers[k] = TriggerInfo(armed.name, k, "error", str(exc))
            self._emit_error(armed, None, str(exc))
            return
        armed.timezone = effective_timezone(profile)
        if wf.on.watch is not None:
            await self._arm_watch(armed)
        if wf.on.schedule is not None:
            self._arm_schedule(armed, wf.on.schedule)
        if wf.on.webhook is not None:
            self._arm_webhook(armed)

    async def _disarm(self, armed: Armed) -> None:
        for task in armed.tasks:
            task.cancel()
        if armed.tasks:
            # asyncio.wait never raises the tasks' CancelledError into the caller, so a
            # cancellation of the caller itself still propagates.
            await asyncio.wait(armed.tasks)
        armed.tasks.clear()

    # -- watch -------------------------------------------------------------------------

    async def _arm_watch(self, armed: Armed) -> None:
        assert armed.workflow is not None
        assert armed.workflow.on is not None
        spec = armed.workflow.on.watch
        assert spec is not None
        assert armed.watch_path is not None
        path = armed.watch_path
        info = TriggerInfo(armed.name, "watch", "armed", path=str(path))
        armed.triggers["watch"] = info
        if not path.is_absolute():
            info.state, info.detail = "error", "watch path must be absolute"
            self._emit_error(armed, "watch", f"watch path {path} is not absolute")
            return
        if not path.is_dir():  # TRIGGERS-3
            info.state, info.detail = "error", "path missing"
            armed.retry_at = self.monotonic() + PATH_RETRY
            self._emit_error(armed, "watch", f"watch path {path} is missing or not a folder")
            return
        polling = spec.poll is not None or self.env.get(FORCE_POLLING_ENV) == "1"
        poll_ms = int(parse_duration(spec.poll) * 1000) if spec.poll else 30_000
        tracker = SettleTracker(parse_duration(spec.settle))
        armed.tracker = tracker
        now = self.monotonic()
        for entry in sorted(path.iterdir()):  # TRIGGERS-11: start-up scan
            if entry.is_dir() and fs.is_inside_entry(path, entry.name):
                tracker.touch(entry.name, now)
        ready = asyncio.Event()
        armed.tasks.append(
            asyncio.create_task(
                self._watch(armed, path, tracker, polling, poll_ms, ready),
                name=f"watch {armed.profile}/{armed.name}",
            )
        )
        await ready.wait()

    async def _watch(
        self,
        armed: Armed,
        root: Path,
        tracker: SettleTracker,
        polling: bool,
        poll_ms: int,
        ready: asyncio.Event,
    ) -> None:
        from watchfiles import Change, awatch

        loose_since: float | None = None
        ready.set()
        try:
            async for changes in awatch(
                root,
                watch_filter=None,
                debounce=200,
                rust_timeout=WATCH_TICK_MS,
                yield_on_timeout=True,
                force_polling=polling,
                poll_delay_ms=poll_ms,
                ignore_permission_denied=True,
            ):
                now = self.monotonic()
                for change, raw in changes:
                    try:
                        rel = os.path.relpath(raw, root)
                    except ValueError:
                        continue
                    top = top_level(rel)
                    if top is None or rel.startswith(".."):
                        continue
                    pending = top in tracker.pending()
                    if change == Change.deleted:
                        if pending:  # a pending candidate still changing (or going away)
                            tracker.touch(top, now)
                        continue  # deletions alone never make a candidate
                    if top == rel and not (root / top).is_dir():
                        loose_since = now  # TRIGGERS-8: a loose file in the watch folder
                        continue
                    if not fs.is_inside_entry(root, top):
                        continue  # SEC-01: a link out of the watch folder is never fired on
                    tracker.touch(top, now)
                    if not pending:  # started settling (DASHBOARD-15)
                        self._inbox_changed(armed, root / top)
                for name in tracker.due(now):
                    folder = root / name
                    if not fs.is_inside_entry(root, name):
                        self._skipped(armed, "watch", str(folder), "outside", None)
                        continue
                    if not folder.is_dir() or not await asyncio.to_thread(fs.has_files, folder):
                        self._inbox_changed(armed, folder)
                        continue  # TRIGGERS-9: gone or empty
                    await self.fire(armed, "watch", str(folder))
                    self._inbox_changed(armed, folder)
                if loose_since is not None and now - loose_since >= tracker.settle:
                    loose_since = None
                    self._emit(
                        armed.profile,
                        "trigger.skipped",
                        {
                            "workflow": armed.name,
                            "kind": "watch",
                            "input": str(root),
                            "reason": "loose_files",
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("watch %s/%s failed", armed.profile, armed.name)
            info = armed.triggers.get("watch")
            if info is not None:
                info.state, info.detail = "error", str(exc)
            armed.retry_at = self.monotonic() + PATH_RETRY
            self._emit_error(armed, "watch", f"watch stopped: {exc}")

    # -- schedule ----------------------------------------------------------------------

    def _arm_schedule(self, armed: Armed, expr: str) -> None:
        from rifferr.core.cron import next_fire

        info = TriggerInfo(armed.name, "schedule", "armed")
        info.next_fire = next_fire(expr, self.now().astimezone(armed.timezone))
        armed.triggers["schedule"] = info
        armed.tasks.append(
            asyncio.create_task(
                self._schedule(armed, expr, info), name=f"schedule {armed.profile}/{armed.name}"
            )
        )

    async def _schedule(self, armed: Armed, expr: str, info: TriggerInfo) -> None:
        from rifferr.core.cron import next_fire

        while True:
            due = info.next_fire
            assert due is not None
            while (left := (due - self.now()).total_seconds()) > 0:
                await self.sleep(min(left, 30.0))  # re-check: wall clocks jump
            # TRIGGERS-13: compute the next time from now, so missed ones aren't caught up
            info.next_fire = next_fire(expr, max(self.now(), due).astimezone(armed.timezone))
            active = self.store.active_run(armed.profile, armed.name, "schedule")
            if active is not None:  # TRIGGERS-14
                self._skipped(armed, "schedule", armed.input, "previous_still_active", active)
                continue
            try:
                await self.fire(armed, "schedule", armed.input)
            except Exception as exc:
                log.exception("scheduled run of %s/%s failed", armed.profile, armed.name)
                self._emit_error(armed, "schedule", f"could not start run: {exc}")

    # -- webhook -----------------------------------------------------------------------

    def _arm_webhook(self, armed: Armed) -> None:
        assert armed.workflow is not None
        assert armed.workflow.on is not None
        hook = armed.workflow.on.webhook
        assert hook is not None
        other = self.webhook_target(armed.profile, hook.name)
        if other is not None and other is not armed:
            armed.triggers["webhook"] = TriggerInfo(
                armed.name, "webhook", "error", f"webhook '{hook.name}' is used by {other.name}"
            )
            return
        armed.triggers["webhook"] = TriggerInfo(armed.name, "webhook", "armed")

    def webhook_target(self, profile: str, name: str) -> Armed | None:
        for armed in self.armed.values():
            wf = armed.workflow
            if (
                armed.profile == profile
                and wf is not None
                and wf.on is not None
                and wf.on.webhook is not None
                and wf.on.webhook.name == name
                and armed.triggers.get("webhook", TriggerInfo("", "webhook", "error")).state
                == "armed"
            ):
                return armed
        return None

    async def handle_webhook(self, profile: str, name: str, payload: Any) -> FireResult | None:
        """Start a run for a webhook call (TRIGGERS-15, -16). ``None``: no input in the
        payload (``200 ignored``). Raises :class:`WebhookError`."""
        from rifferr.core.triggers import lookup

        armed = self.webhook_target(profile, name)
        if armed is None or armed.workflow is None or armed.workflow.on is None:
            raise WebhookError(404, "not_found", "no such webhook")
        hook = armed.workflow.on.webhook
        assert hook is not None
        assert armed.within is not None
        value = lookup(payload, hook.input)
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise WebhookError(422, "bad_input", f"'{hook.input}' is not a path")
        try:
            folder = await asyncio.to_thread(fs.confine, value, armed.within)
        except fs.OutsideError as exc:
            raise WebhookError(422, "bad_input", str(exc)) from None
        return await self.fire(armed, "webhook", str(folder))

    # -- firing ------------------------------------------------------------------------

    def _called(self, armed: Armed) -> dict[str, str] | None:
        """The called files' texts read and checked when it was armed: the run keeps
        these (SUBWORKFLOW-12, SEC-61)."""
        return armed.calls.texts() if armed.calls is not None else None

    def _fire_problem(self, armed: Armed) -> str | None:
        """Why ``armed`` must not submit a run now (SEC-61): it is in error, or it or a
        file it calls is not trusted in its profile."""
        if armed.error is not None:
            return armed.error
        texts = [armed.text, *(self._called(armed) or {}).values()]
        if not armed.trusted or not all(
            self.db.is_trusted(workflow_hash(t), armed.profile) for t in texts
        ):
            return "untrusted: the workflow or a file it calls is not trusted; not run"
        return None

    async def fire(
        self, armed: Armed, kind: FireKind, input_path: str, *, force: bool = False
    ) -> FireResult:
        """Deduplicate and submit one run (TRIGGERS-19 .. -24)."""
        problem = self._fire_problem(armed)
        if problem is not None:  # SEC-61: never submit a run that would be untrusted
            self._emit_error(armed, None if kind == "manual" else kind, problem)
            return FireResult(None)
        async with self._fire_lock:  # one decision at a time per server
            fp = None
            if input_path:
                fp = await asyncio.to_thread(fs.input_fingerprint, Path(input_path))
            record = self.store.record(armed.profile, armed.name, input_path) if fp else None
            reason = dedup_skip(record, fp, force=force or kind == "manual")
            if reason is None and kind == "webhook" and input_path:  # SEC-82
                busy = self._active_related(armed, input_path)
                if busy is not None:
                    self._skipped(armed, kind, input_path, "in_progress", busy)
                    return FireResult(busy, "in_progress")
            if reason is not None:
                assert record is not None
                self._skipped(armed, kind, input_path, reason, record.run_id)
                return FireResult(record.run_id, reason)
            run_id = await self.submit(
                armed.profile,
                armed.text,
                source_name=str(armed.path),
                workflow=armed.name,
                trigger=kind,
                run_input=input_path or None,
                called=self._called(armed),
            )
            if fp is not None:
                self.store.remember(armed.profile, armed.name, input_path, fp, run_id)
        self._emit(
            armed.profile,
            "trigger.fired",
            {"workflow": armed.name, "kind": kind, "input": input_path, "run_id": run_id},
        )
        return FireResult(run_id)

    def _active_related(self, armed: Armed, input_path: str) -> str | None:
        """SEC-82: the run id of an active run of this workflow whose input is a parent
        or child folder of ``input_path``."""
        here = Path(input_path)
        for other, rec in self.store.records(armed.profile, armed.name).items():
            if rec.run_state not in ACTIVE_STATES:
                continue
            there = Path(other)
            if there != here and (here.is_relative_to(there) or there.is_relative_to(here)):
                return rec.run_id
        return None

    def remember_manual(self, profile: str, workflow: str, input_path: str, run_id: str) -> None:
        """TRIGGERS-21: a manual run records its input's fingerprint."""
        if not input_path:
            return
        fp = fs.input_fingerprint(Path(input_path)) if Path(input_path).is_dir() else None
        if fp is not None:
            self.store.remember(profile, workflow, input_path, fp, run_id)

    # -- listing and events ------------------------------------------------------------

    def list(self, profile: str) -> list[TriggerInfo]:
        out: list[TriggerInfo] = []
        for (p, _), armed in sorted(self.armed.items()):
            if p == profile:
                out.extend(armed.triggers[k] for k in sorted(armed.triggers))
        return out

    def _skipped(
        self, armed: Armed, kind: str, input_path: str, reason: str, run_id: str | None
    ) -> None:
        data: dict[str, JsonValue] = {
            "workflow": armed.name,
            "kind": kind,
            "input": input_path,
            "reason": reason,
        }
        if run_id is not None:
            data["run_id"] = run_id
        self._emit(armed.profile, "trigger.skipped", data)

    def _inbox_changed(self, armed: Armed, path: Path) -> None:
        self._emit(armed.profile, "inbox.changed", {"workflow": armed.name, "path": str(path)})

    async def inbox(self, profile: str, *, limit: int = INBOX_LIMIT) -> list[InboxFolder]:
        """Each watch trigger's folder and its candidate inputs (DASHBOARD-1, -2). The
        folders are read off the event loop; the app DB on it."""
        out: list[InboxFolder] = []
        mono, wall = self.monotonic(), self.now()
        for (p, _), armed in sorted(self.armed.items()):
            info = armed.triggers.get("watch")
            if p != profile or info is None or armed.watch_path is None:
                continue
            folder = InboxFolder(armed.name, str(armed.watch_path), info.state, info.detail)
            out.append(folder)
            listed = await asyncio.to_thread(_list_inputs, armed.watch_path, limit)
            if listed is None:
                continue
            entries, folder.omitted = listed
            records = self.store.records(profile, armed.name)
            for name, inside, current in entries:
                record = records.get(str(armed.watch_path / name))
                if not inside:
                    folder.entries.append(InboxItem(name, "outside", None, None))
                    continue
                last = armed.tracker.last(name) if armed.tracker is not None else None
                changed = wall - timedelta(seconds=mono - last) if last is not None else None
                state = inbox_state(record, current, settling=last is not None)
                run_id = record.run_id if record is not None else None
                folder.entries.append(InboxItem(name, state, changed, run_id))
        return out

    def _emit_error(self, armed: Armed, kind: Kind | None, message: str) -> None:
        self._emit(
            armed.profile,
            "trigger.error",
            {"workflow": armed.name, "kind": kind, "message": message},
        )

    def _emit(self, profile: str, type_: str, data: dict[str, JsonValue]) -> None:
        try:
            event = self.db.append_next(profile_stream(profile), type_, data)
        except Exception:
            log.exception("could not store %s", type_)
            return
        log.info("%s %s", type_, data)
        self.publish(event)
