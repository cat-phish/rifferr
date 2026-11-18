"""The dashboard's inbox listing from the trigger manager (F-DASHBOARD DASHBOARD-1, -2,
-15)."""

from __future__ import annotations

import asyncio

from rifferr.appdb import AppTriggers
from rifferr.core.triggers import DedupRecord, inbox_state
from tests.triggers.test_manager import Env, env, wait_for, watch_wf

__all__ = ["env"]


async def states(e: Env) -> dict[str, str]:
    (folder,) = await e.manager.inbox("main")
    return {i.path: i.state for i in folder.entries}


async def until_states(e: Env, want: dict[str, str]) -> None:
    for _ in range(160):
        if await states(e) == want:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"inbox never became {want}: {await states(e)}")


def test_DASHBOARD_1_inbox_state_rules() -> None:
    rec = DedupRecord("fp", "r1", "succeeded")
    assert inbox_state(None, "fp", settling=True) == "settling"
    assert inbox_state(None, "fp", settling=False) == "waiting"
    assert inbox_state(DedupRecord("fp", "r1", "queued"), "fp", settling=False) == "waiting"
    assert inbox_state(DedupRecord("fp", "r1", "running"), "x", settling=False) == "running"
    assert (
        inbox_state(DedupRecord("fp", "r1", "waiting_review"), None, settling=False)
        == "waiting_review"
    )
    assert inbox_state(rec, "fp", settling=False) == "imported"
    assert inbox_state(rec, "other", settling=False) == "changed"
    assert inbox_state(DedupRecord("fp", "r1", "failed"), "fp", settling=False) == "failed"
    assert inbox_state(DedupRecord("fp", "r1", "succeeded", True), "fp", settling=False) == (
        "undone"
    )


async def test_DASHBOARD_1_inbox_lists_watched_inputs_with_their_state(env: Env) -> None:
    env.album("Done")
    env.write("w", watch_wf(settle="1s"))
    await env.manager.start()
    await wait_for(lambda: len(env.runs) == 1)
    assert await states(env) == {"Done": "waiting"}  # queued
    env.finish(env.runs[0].run_id, "succeeded")
    assert await states(env) == {"Done": "imported"}
    (folder,) = await env.manager.inbox("main")
    assert (folder.workflow, folder.folder, folder.trigger_state) == ("w", str(env.inbox), "armed")
    assert folder.entries[0].run_id == env.runs[0].run_id
    AppTriggers(env.db).mark_undone(env.runs[0].run_id)
    assert await states(env) == {"Done": "undone"}
    (env.inbox / "Done" / "extra.flac").write_bytes(b"new")
    assert (await states(env))["Done"] in ("settling", "changed")


async def test_DASHBOARD_1_settling_input_shows_when_it_changed(env: Env) -> None:
    env.write("w", watch_wf(settle="1h"))
    await env.manager.start()
    env.album("New")
    await until_states(env, {"New": "settling"})
    (folder,) = await env.manager.inbox("main")
    assert folder.entries[0].changed_at is not None


async def test_DASHBOARD_15_new_input_emits_inbox_changed(env: Env) -> None:
    env.write("w", watch_wf(settle="1h"))
    await env.manager.start()
    env.album("New")
    await wait_for(lambda: any(e.type == "inbox.changed" for e in env.events))
    changed = [e for e in env.events if e.type == "inbox.changed"]
    assert changed[0].data == {"workflow": "w", "path": str(env.inbox / "New")}
    assert len(changed) == 1  # further changes while settling are coalesced


async def test_DASHBOARD_2_inbox_caps_entries_and_marks_links_outside(
    env: Env, tmp_path: object
) -> None:
    outside = env.inbox.parent / "elsewhere"
    outside.mkdir()
    (env.inbox / "link").symlink_to(outside, target_is_directory=True)
    for name in ("A", "B", "C"):
        (env.inbox / name).mkdir()
    env.write("w", watch_wf(settle="1h"))
    await env.manager.start()
    (folder,) = await env.manager.inbox("main", limit=3)
    assert [(i.path, i.state) for i in folder.entries] == [
        ("A", "settling"),
        ("B", "settling"),
        ("C", "settling"),
    ]
    assert folder.omitted == 1
    (full,) = await env.manager.inbox("main")
    assert ("link", "outside") in [(i.path, i.state) for i in full.entries]
