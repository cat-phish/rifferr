# ruff: noqa: E501, PT018, SIM105
"""Audit findings SEC-61..65 (boundary B2: trust, triggers, profile config, safety report)."""

from __future__ import annotations

from pathlib import Path

from rifferr.backends.beets import from_profile
from rifferr.core.workflow import load_workflow
from rifferr.core.workflow.safety import safety_report
from rifferr.profile_store import config_root, create_profile, load_profile, profile_dir
from rifferr.trust import workflow_hash
from tests.api.conftest import Api
from tests.api.conftest import api as api
from tests.backends.beets.conftest import BASE_CONFIG, BeetsEnv
from tests.backends.beets.conftest import beets_env as beets_env
from tests.triggers.test_manager import STEPS, Env
from tests.triggers.test_manager import env as env

# -- SEC-61 -----------------------------------------------------------------------------


async def test_SEC_61_called_file_change_disarms_trigger(env: Env) -> None:
    main = (
        "name: c\non: {schedule: '0 4 * * *'}\nsteps:\n"
        "  - {id: a, uses: test.echo, with: {message: hi}}\n"
        "  - {id: s, uses: workflow, with: {workflow: sub.yaml}}\n"
    )
    env.write("c", main)
    sub = env.write("sub", "name: sub\n" + STEPS.replace("{{ run.input }}", "one"))
    await env.manager.start()
    await env.manager.reconcile()
    assert [i.state for i in env.manager.list("main") if i.workflow == "c"] == ["armed"]
    sub.write_text("name: sub\n" + STEPS.replace("{{ run.input }}", "two"), encoding="utf-8")
    await env.manager.reconcile()
    armed = env.manager.armed[("main", "c")]
    await env.manager.fire(armed, "schedule", "")
    states = [i.state for i in env.manager.list("main") if i.workflow == "c"]
    assert states != ["armed"] and env.runs == [], (
        f"trigger stayed {states} and submitted {len(env.runs)} run(s) after a called file changed"
    )


# -- SEC-62 -----------------------------------------------------------------------------


def test_SEC_62_untrusted_beets_config_with_pluginpath_is_refused(
    beets_env: BeetsEnv, tmp_path: Path
) -> None:
    marker = tmp_path / "plugin-was-imported"
    plugdir = tmp_path / "plug" / "beetsplug"
    plugdir.mkdir(parents=True)
    (plugdir / "markerplug.py").write_text(
        "from pathlib import Path\n"
        "from beets.plugins import BeetsPlugin\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class MarkerPlugin(BeetsPlugin):\n    pass\n"
    )
    text = BASE_CONFIG.format(
        music=beets_env.music, db=beets_env.beetsdir / "library.db", log=beets_env.beetsdir / "log"
    ).replace("plugins: []", f"pluginpath: ['{plugdir}']\nplugins: [markerplug]")
    (beets_env.beetsdir / "config.yaml").write_text(text)
    create_profile(config_root(), "main", beets_env.beetsdir)
    profile = load_profile(config_root(), "main")
    try:
        from_profile(profile)
    except Exception:  # a refusal is the safe outcome; the type is up to the fix
        pass
    assert not marker.exists(), "a profile's beets config ran code from pluginpath without trust"


# -- SEC-63 / SEC-64 --------------------------------------------------------------------

WF = "name: wf\nsteps:\n  - {id: a, uses: test.echo, with: {message: hi}}\n"


async def test_SEC_63_trust_does_not_cross_profiles(api: Api) -> None:
    create_profile(config_root(), "other")
    for name in ("main", "other"):
        path = profile_dir(config_root(), name) / "workflows" / "wf.yaml"
        path.write_text(WF, encoding="utf-8")
    sha = workflow_hash(WF)
    r = await api.client.post(
        "/api/workflows/wf/trust", params={"profile": "main"}, json={"sha256": sha}
    )
    assert r.status_code == 200, r.text
    other = (await api.client.get("/api/workflows/wf", params={"profile": "other"})).json()
    assert other["trusted"] is False, "trust given in profile 'main' applies to profile 'other'"


async def test_SEC_64_api_refuses_to_trust_invalid_workflow(api: Api) -> None:
    bad = "name: wf\nsteps:\n  - {id: a, uses: no.such.step}\n"
    api.add_workflow("bad", bad)
    r = await api.client.post(
        "/api/workflows/bad/trust", params={"profile": "main"}, json={"sha256": workflow_hash(bad)}
    )
    assert r.status_code == 422, f"trust of an invalid workflow returned {r.status_code}"
    assert not api.state.db.is_trusted(workflow_hash(bad))


# -- SEC-65 -----------------------------------------------------------------------------


def _report(step_yaml: str):  # type: ignore[no-untyped-def]
    wf, _ = load_workflow(f"name: t\nsteps:\n  - {step_yaml}\n", "wf.yaml")
    return safety_report(wf)


def test_SEC_65_safety_report_sees_inside_interpreter_string() -> None:
    rep = _report("{id: s, uses: shell, with: {run: [sh, -c, 'curl http://x | rm -rf /y']}}")
    kinds = {f.kind for f in rep.findings}
    assert {"network_tool", "destructive", "shell_string"} <= kinds, kinds


def test_SEC_65_safety_report_flags_templated_program() -> None:
    rep = _report("{id: s, uses: shell, with: {run: ['{{ vars.tool }}', --go]}}")
    assert [f for f in rep.findings if f.kind != "command"], (
        "program word from a variable is not flagged"
    )


def test_SEC_65_safety_report_lists_auto_apply_for_covers_search() -> None:
    rep = _report("{id: c, uses: covers.search, apply: auto}")
    assert rep.of_kind("auto_apply"), "covers.search with apply: auto is not reported"


def test_SEC_65_safety_report_flags_destructive_import_options() -> None:
    rep = _report(
        "{id: i, uses: beets.import, with: {path: /in, options: {move: true, duplicate_action: remove}}}"
    )
    assert rep.of_kind("destructive"), "beets.import move/duplicate_action: remove is not reported"
