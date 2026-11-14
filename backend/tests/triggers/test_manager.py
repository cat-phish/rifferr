"""F-TRIGGERS: the trigger manager against a temp config root and inbox.

Runs are submitted to a fake ``submit`` that records them as queued jobs in the app DB,
so dedup sees real job states. Watch tests use real file events in ``tmp_path``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppDB
from rifferr.engine.events import Event
from rifferr.profile_store import WORKFLOWS_DIR, create_profile, profile_dir
from rifferr.triggers import TriggerManager, WebhookError
from rifferr.trust import trust_file
from tests.worker.fake_worker import registry

STEPS = "steps:\n  - {id: a, uses: test.echo, with: {message: '{{ run.input }}'}}\n"


@dataclass
class Submitted:
    profile: str
    workflow: str | None
    trigger: str
    input: str | None
    run_id: str


@dataclass
class Env:
    root: Path
    inbox: Path
    db: AppDB
    manager: TriggerManager
    runs: list[Submitted] = field(default_factory=list[Submitted])
    events: list[Event] = field(default_factory=list[Event])

    def write(self, name: str, text: str, *, trust: bool = True) -> Path:
        path = profile_dir(self.root, "main") / WORKFLOWS_DIR / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        if trust:
            trust_file(self.db, path, "test")
        return path

    def finish(self, run_id: str, state: str) -> None:
        self.db._conn.execute(  # pyright: ignore[reportPrivateUsage]
            "UPDATE jobs SET state = ? WHERE run_id = ?", (state, run_id)
        )

    def types(self) -> list[tuple[str, Any]]:
        return [(e.type, e.data.get("reason")) for e in self.events]

    def album(self, name: str, files: int = 2) -> Path:
        folder = self.inbox / name
        folder.mkdir()
        for i in range(files):
            (folder / f"{i:02}.flac").write_bytes(b"x" * (i + 1))
        return folder


async def wait_for(cond: Callable[[], bool], timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("timed out")
        await asyncio.sleep(0.05)


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    root = tmp_path / "config"
    create_profile(root, "main")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pfile = profile_dir(root, "main") / "profile.yaml"
    pfile.write_text(
        pfile.read_text().replace("vars: {}", f"vars: {{inbox: '{inbox}'}}"), encoding="utf-8"
    )
    db = AppDB(tmp_path / "app.db")
    holder: dict[str, Env] = {}

    async def submit(
        profile: str,
        source: str,
        *,
        source_name: str,
        workflow: str | None = None,
        trigger: str = "manual",
        run_input: str | None = None,
    ) -> str:
        job = db.submit(profile, {"source": source}, workflow=workflow, trigger=trigger)
        holder["env"].runs.append(Submitted(profile, workflow, trigger, run_input, job.run))
        return job.run

    manager = TriggerManager(
        config_root=root,
        db=db,
        registry=registry(),
        submit=submit,
        reconcile_interval=0.1,
        env={},
    )
    e = Env(root, inbox, db, manager)
    manager.publish = e.events.append
    holder["env"] = e
    yield e
    await manager.stop()
    db.close()


def watch_wf(settle: str = "1s", extra: str = "") -> str:
    return f"name: w\non:\n  watch: {{path: '{{{{ vars.inbox }}}}', settle: {settle}}}\n{extra}" + (
        STEPS
    )


# -- arming -----------------------------------------------------------------------------


async def test_TRIGGERS_2_untrusted_workflow_is_not_armed(env: Env) -> None:
    path = env.write("w", watch_wf(), trust=False)
    await env.manager.start()
    (info,) = env.manager.list("main")
    assert (info.kind, info.state, info.detail) == ("watch", "disabled", "untrusted")
    env.album("A")
    await asyncio.sleep(1.5)
    assert env.runs == []
    trust_file(env.db, path, "test")  # TRIGGERS-2: trusting arms it without a restart
    await wait_for(lambda: len(env.runs) == 1)
    assert env.manager.list("main")[0].state == "armed"


async def test_TRIGGERS_1_edit_rearms_and_resets_trust(env: Env) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    assert env.manager.list("main")[0].state == "armed"
    env.write("w", watch_wf(settle="2s"), trust=False)
    await wait_for(lambda: env.manager.list("main")[0].state == "disabled")


async def test_TRIGGERS_1_deleted_workflow_is_disarmed(env: Env) -> None:
    path = env.write("w", watch_wf())
    await env.manager.start()
    path.unlink()
    await wait_for(lambda: env.manager.list("main") == [])


async def test_TRIGGERS_1_invalid_workflow_reports_error(env: Env) -> None:
    env.write("w", "name: w\non: {schedule: '0 4 * * *'}\nsteps:\n  - {id: a, uses: nope}\n")
    await env.manager.start()
    (info,) = env.manager.list("main")
    assert info.state == "error"
    assert "unknown step type" in (info.detail or "")
    assert ("trigger.error", None) in env.types()


async def test_TRIGGERS_3_missing_watch_path_is_an_error(env: Env) -> None:
    env.write("w", "name: w\non: {watch: {path: /nonexistent/inbox}}\n" + STEPS)
    await env.manager.start()
    (info,) = env.manager.list("main")
    assert (info.state, info.detail) == ("error", "path missing")


async def test_TRIGGERS_4_list_shows_resolved_path_and_next_fire(env: Env) -> None:
    env.write(
        "w",
        "name: w\non:\n  watch: {path: '{{ vars.inbox }}'}\n  schedule: '0 4 * * *'\n" + STEPS,
    )
    await env.manager.start()
    by_kind = {i.kind: i for i in env.manager.list("main")}
    assert by_kind["watch"].path == str(env.inbox)
    assert by_kind["schedule"].next_fire is not None


# -- watch ------------------------------------------------------------------------------


async def test_TRIGGERS_9_watch_starts_a_run_once_a_folder_settles(env: Env) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    folder = env.album("Artist - Album")
    await wait_for(lambda: len(env.runs) == 1)
    run = env.runs[0]
    assert (run.trigger, run.input, run.workflow) == ("watch", str(folder), "w")
    assert ("trigger.fired", None) in env.types()


async def test_TRIGGERS_9_changes_reset_the_settle_timer(env: Env) -> None:
    env.write("w", watch_wf(settle="2s"))
    await env.manager.start()
    folder = env.album("A", files=1)
    for i in range(4):  # keep writing for ~2.4 s
        await asyncio.sleep(0.6)
        (folder / f"late{i}.flac").write_bytes(b"y")
    assert env.runs == []
    await wait_for(lambda: len(env.runs) == 1)


async def test_TRIGGERS_9_empty_folder_starts_nothing(env: Env) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    (env.inbox / "empty").mkdir()
    await asyncio.sleep(2.0)
    assert env.runs == []


async def test_TRIGGERS_8_loose_files_are_reported_not_run(env: Env) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    (env.inbox / "track.flac").write_bytes(b"x")
    await wait_for(lambda: ("trigger.skipped", "loose_files") in env.types())
    assert env.runs == []


async def test_TRIGGERS_11_startup_scan_and_dedup(env: Env) -> None:
    env.album("Old")
    env.write("w", watch_wf())
    await env.manager.start()
    await wait_for(lambda: len(env.runs) == 1)
    env.finish(env.runs[0].run_id, "succeeded")
    await env.manager.stop()  # restart: the scan finds "Old" again, unchanged
    env.manager._stopping = False  # pyright: ignore[reportPrivateUsage]
    await env.manager.start()
    await wait_for(lambda: ("trigger.skipped", "already_run") in env.types())
    assert len(env.runs) == 1


async def test_TRIGGERS_20_changed_folder_runs_again(env: Env) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    folder = env.album("A")
    await wait_for(lambda: len(env.runs) == 1)
    env.finish(env.runs[0].run_id, "succeeded")
    (folder / "bonus.flac").write_bytes(b"bonus")
    await wait_for(lambda: len(env.runs) == 2)


async def test_TRIGGERS_9_deletions_alone_start_nothing(env: Env) -> None:
    folder = env.album("A", files=3)
    env.write("w", watch_wf())
    await env.manager.start()
    await wait_for(lambda: len(env.runs) == 1)
    env.finish(env.runs[0].run_id, "succeeded")
    (folder / "00.flac").unlink()  # e.g. beets moved the files away
    await asyncio.sleep(2.0)
    assert len(env.runs) == 1


async def test_TRIGGERS_25_stop_drops_settle_timers(env: Env) -> None:
    env.write("w", watch_wf(settle="2s"))
    await env.manager.start()
    env.album("A")
    await asyncio.sleep(0.5)
    await env.manager.stop()
    await asyncio.sleep(2.0)
    assert env.runs == []


# -- dedup ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "reason"),
    [("running", "in_progress"), ("succeeded", "already_run"), ("failed", "previously_failed")],
)
async def test_TRIGGERS_19_same_input_is_skipped(env: Env, state: str, reason: str) -> None:
    env.write("w", watch_wf())
    await env.manager.start()
    folder = env.album("A")
    armed = env.manager.armed[("main", "w")]
    await wait_for(lambda: len(env.runs) == 1)
    env.finish(env.runs[0].run_id, state)
    result = await env.manager.fire(armed, "webhook", str(folder))
    assert result.skipped == reason
    assert result.run_id == env.runs[0].run_id
    forced = await env.manager.fire(armed, "webhook", str(folder), force=True)
    assert forced.skipped is None
    assert len(env.runs) == 2


async def test_TRIGGERS_21_manual_run_records_its_fingerprint(env: Env) -> None:
    env.write("w", watch_wf(settle="30s"))
    await env.manager.start()
    folder = env.album("A")
    job = env.db.submit("main", {}, workflow="w", trigger="manual", input=str(folder))
    env.manager.remember_manual("main", "w", str(folder), job.run)
    result = await env.manager.fire(env.manager.armed[("main", "w")], "watch", str(folder))
    assert result.skipped == "in_progress"


# -- schedule ---------------------------------------------------------------------------


class Clock:
    """Fake wall clock: ``sleep`` advances it, and blocks for real once ``until`` is
    reached, so a test sees the state right after the firings it expects."""

    def __init__(self, start: datetime, until: datetime | None = None) -> None:
        self.t = start
        self.until = until

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        if self.until is not None and self.t >= self.until:
            await asyncio.sleep(3600)
        self.t += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def utc_profile(env: Env) -> None:
    pfile = profile_dir(env.root, "main") / "profile.yaml"
    pfile.write_text(pfile.read_text() + "timezone: UTC\n", encoding="utf-8")


async def test_TRIGGERS_12_schedule_fires_with_workflow_input(env: Env) -> None:
    start = datetime(2026, 1, 10, 3, 59, 50, tzinfo=UTC)
    clock = Clock(start, until=start + timedelta(seconds=10))
    env.manager.now, env.manager.sleep = clock.now, clock.sleep
    utc_profile(env)
    env.write("s", "name: s\ninput: '{{ vars.inbox }}'\non: {schedule: '0 4 * * *'}\n" + STEPS)
    env.album("A")
    await env.manager.start()
    await wait_for(lambda: len(env.runs) == 1)
    assert (env.runs[0].trigger, env.runs[0].input) == ("schedule", str(env.inbox))
    (info,) = env.manager.list("main")
    assert info.next_fire == datetime(2026, 1, 11, 4, 0, tzinfo=UTC)


async def test_TRIGGERS_14_schedule_skips_while_previous_active(env: Env) -> None:
    clock = Clock(datetime(2026, 1, 10, 3, 59, 0, tzinfo=UTC))
    env.manager.now, env.manager.sleep = clock.now, clock.sleep
    env.write("s", "name: s\non: {schedule: '* * * * *'}\n" + STEPS)
    await env.manager.start()
    await wait_for(lambda: len(env.runs) == 1)
    await wait_for(lambda: ("trigger.skipped", "previous_still_active") in env.types())
    env.finish(env.runs[0].run_id, "succeeded")
    await wait_for(lambda: len(env.runs) == 2)


async def test_TRIGGERS_13_missed_firings_are_not_caught_up(env: Env) -> None:
    clock = Clock(datetime(2026, 1, 10, 5, 0, tzinfo=UTC))  # 04:00 was missed
    env.manager.now = clock.now
    env.manager.sleep = lambda _s: asyncio.sleep(3600)  # nothing becomes due
    utc_profile(env)
    env.write("s", "name: s\non: {schedule: '0 4 * * *'}\n" + STEPS)
    await env.manager.start()
    await asyncio.sleep(0.3)
    assert env.runs == []
    (info,) = env.manager.list("main")
    assert info.next_fire is not None
    assert info.next_fire.date().day == 11


# -- webhook ----------------------------------------------------------------------------

HOOK = "name: h\non: {webhook: {name: dl, input: files.0.path, within: '{{ vars.inbox }}'}}\n"


async def test_TRIGGERS_15_webhook_payload_path_becomes_the_input(env: Env) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    folder = env.album("A")
    result = await env.manager.handle_webhook(
        "main", "dl", {"files": [{"path": str(folder / "00.flac")}]}
    )
    assert result is not None
    assert result.run_id == env.runs[0].run_id
    assert (env.runs[0].trigger, env.runs[0].input) == ("webhook", str(folder))


async def test_TRIGGERS_16_payload_without_input_is_ignored(env: Env) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    assert await env.manager.handle_webhook("main", "dl", {"eventType": "Test"}) is None
    assert env.runs == []


@pytest.mark.parametrize("target", ["/etc", "../outside", "relative/path", "inbox-root"])
async def test_TRIGGERS_16_paths_outside_within_are_refused(
    env: Env, tmp_path: Path, target: str
) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    (tmp_path / "outside").mkdir()
    path = {
        "../outside": str(env.inbox / ".." / "outside"),
        "inbox-root": str(env.inbox),
    }.get(target, target)
    with pytest.raises(WebhookError) as exc:
        await env.manager.handle_webhook("main", "dl", {"files": [{"path": path}]})
    assert (exc.value.status, exc.value.code) == (422, "bad_input")
    assert env.runs == []


async def test_TRIGGERS_16_symlink_out_of_within_is_refused(env: Env, tmp_path: Path) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    outside = tmp_path / "secret"
    outside.mkdir()
    (env.inbox / "link").symlink_to(outside)
    with pytest.raises(WebhookError):
        await env.manager.handle_webhook(
            "main", "dl", {"files": [{"path": str(env.inbox / "link")}]}
        )


async def test_TRIGGERS_15_unknown_or_untrusted_webhook_is_404(env: Env) -> None:
    env.write("h", HOOK + STEPS, trust=False)
    await env.manager.start()
    with pytest.raises(WebhookError) as exc:
        await env.manager.handle_webhook("main", "dl", {})
    assert exc.value.status == 404
    with pytest.raises(WebhookError):
        await env.manager.handle_webhook("main", "other", {})


async def test_TRIGGERS_15_duplicate_webhook_name_is_an_error(env: Env) -> None:
    env.write("h", HOOK + STEPS)
    env.write("h2", HOOK.replace("name: h\n", "name: h2\n") + STEPS)
    await env.manager.start()
    states = {i.workflow: i.state for i in env.manager.list("main")}
    assert states == {"h": "armed", "h2": "error"}
