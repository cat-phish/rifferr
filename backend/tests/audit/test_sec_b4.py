# ruff: noqa: F811, PT018
"""Audit B4 (triggers / webhooks) findings SEC-81..87. Each test asserts the SAFE behavior."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from rifferr.api.app import ApiSettings  # noqa: F401
from rifferr.backends.fake import FakeBackend
from rifferr.core.workflow import load_workflow
from rifferr.engine import Engine, InMemoryEventSink, RunState, StepTypeRegistry
from rifferr.profile_store import profile_dir
from rifferr.steps.beets_import import BeetsImportStep
from rifferr.triggers import WebhookError
from tests.api.conftest import Api, api  # noqa: F401  (fixture)
from tests.api.test_hooks import album, caller, hooked  # noqa: F401  (fixture)
from tests.cli.conftest import cli, make_profile
from tests.triggers.test_manager import HOOK, STEPS, Env, env  # noqa: F401  (fixture)

IMPORT_WF = "name: i\nsteps:\n  - {id: imp, uses: beets.import, with: {path: '{{ run.input }}'}}\n"


async def test_SEC_81_webhook_input_rechecked_at_step_time(env: Env, tmp_path: Path) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    a = env.album("A")
    result = await env.manager.handle_webhook("main", "dl", {"files": [{"path": str(a)}]})
    assert result is not None and result.run_id is not None
    submitted = env.runs[0].input
    assert submitted == str(a)
    # gap between the check and the run: A becomes a link to a folder outside `within`
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "01.flac").write_bytes(b"x")
    for child in a.iterdir():
        child.unlink()
    a.rmdir()
    a.symlink_to(outside)

    backend = FakeBackend.from_fixture({"albums": []})
    backend.add_import_fixture(a, [{"album": "Evil", "artist": "X"}])
    text = IMPORT_WF
    wf, doc = load_workflow(text, "wf.yaml")
    engine = Engine(StepTypeRegistry([BeetsImportStep()]), InMemoryEventSink())
    run = engine.create_run(
        wf, text.encode(), profile_name="main", profile_vars={}, backend=backend,
        run_input=submitted, trigger="webhook", doc=doc, trusted=True,
    )  # fmt: skip
    await engine.execute(run)
    assert run.state is RunState.failed
    assert backend.import_calls == []


async def test_SEC_82_webhook_subfolder_of_active_input_is_skipped(env: Env) -> None:
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    a = env.album("A")
    (a / "CD1").mkdir()
    (a / "CD1" / "x.flac").write_bytes(b"x")
    armed = env.manager.armed[("main", "h")]
    first = await env.manager.fire(armed, "watch", str(a))
    assert first.skipped is None
    second = await env.manager.handle_webhook("main", "dl", {"files": [{"path": str(a / "CD1")}]})
    assert second is not None
    assert second.skipped == "in_progress"
    assert second.run_id == first.run_id


async def test_SEC_83_hook_auth_failure_log_has_no_raw_newline(
    api: Api,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    async with caller(api) as c:
        r = await c.post("/hooks/main/x%0AFORGED%0D%1B[31m", content=b"{}")
    assert r.status_code == 401
    for rec in caplog.records:
        msg = rec.getMessage()
        assert "\n" not in msg and "\r" not in msg and "\x1b" not in msg, repr(msg)


async def test_SEC_84_hook_bad_payloads_answer_4xx(hooked) -> None:  # type: ignore[no-untyped-def]
    api, inbox, secret = hooked
    headers = {"authorization": f"Bearer {secret}", "content-type": "application/json"}
    async with caller(api) as c:
        nul = await c.post(
            "/hooks/main/dl", json={"files": [{"path": str(inbox) + "/a\u0000b"}]}, headers=headers
        )
    assert nul.status_code == 422, nul.status_code


async def test_SEC_84_hook_deeply_nested_body_answers_400(hooked) -> None:  # type: ignore[no-untyped-def]
    api, _inbox, secret = hooked
    headers = {"authorization": f"Bearer {secret}", "content-type": "application/json"}
    async with caller(api) as c:
        deep = await c.post(
            "/hooks/main/dl", content=b"[" * 200_000 + b"]" * 200_000, headers=headers
        )
    assert deep.status_code == 400, deep.status_code


async def test_SEC_85_webhook_within_must_be_absolute_after_interpolation(
    env: Env,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pfile = profile_dir(env.root, "main") / "profile.yaml"
    pfile.write_text(
        pfile.read_text().replace(f"vars: {{inbox: '{env.inbox}'}}", "vars: {inbox: ''}")
    )
    cwd = tmp_path / "cwd"
    (cwd / "sub").mkdir(parents=True)
    monkeypatch.chdir(cwd)
    env.write("h", HOOK + STEPS)
    await env.manager.start()
    (info,) = env.manager.list("main")
    assert info.state == "error", (info.state, info.detail)
    with pytest.raises(WebhookError) as exc:
        await env.manager.handle_webhook("main", "dl", {"files": [{"path": str(cwd / "sub")}]})
    assert exc.value.status == 404
    assert env.runs == []


async def test_SEC_86_webhook_name_with_trailing_newline_refused_route(api: Api) -> None:
    r = await api.client.post("/api/profiles/main/webhooks/hook%0A/secret", json={})
    assert r.status_code == 400, r.text


def test_SEC_86_webhook_name_with_trailing_newline_refused_cli(tmp_path: Path) -> None:
    make_profile(tmp_path)
    assert cli("webhook", "new", "hook\n", "--profile", "main") == 2


async def test_SEC_87_secret_route_does_not_replace_without_flag(api: Api) -> None:
    from rifferr.appdb import AppTriggers
    from rifferr.triggers.secrets import verify

    url = "/api/profiles/main/webhooks/dl/secret"
    first = await api.client.post(url, json={})
    assert first.status_code == 201
    assert "no-store" in first.headers.get("cache-control", "")
    secret = first.json()["secret"]
    second = await api.client.post(url, json={})
    assert second.status_code == 409, second.status_code
    assert verify(AppTriggers(api.state.db), "main", "dl", secret)
