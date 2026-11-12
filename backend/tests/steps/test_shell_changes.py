"""S-SHELL M1 update: ``emits: changes`` (SHELL-11, -14 .. -19)."""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from rifferr.core.selection import Selection
from rifferr.engine import RunState
from rifferr.steps import shell
from tests.steps.conftest import ShellHarness
from tests.steps.test_shell import errors, step

PY = sys.executable


def script(code: str, **with_: Any) -> dict[str, Any]:
    return {"run": [PY, "-c", code], "emits": "changes", **with_}


def printing(proposal: Any, **with_: Any) -> dict[str, Any]:
    """A script that prints ``proposal`` (as JSON) whatever its input."""
    return script(f"print({json.dumps(json.dumps(proposal))})", **with_)


def proposed(sh: ShellHarness, run: Any) -> list[Any]:
    return [s.changeset for s in sh.engine.changesets.for_run(run.id)]


def info(sh: ShellHarness, run: Any) -> list[str]:
    return sh.lines(run, "info")


def album_id(sh: ShellHarness, name: str) -> str:
    return next(a.id for a in sh.backend.query(Selection()) if a.fields["album"] == name)


EP_TYPE = """
import json, sys
album = json.load(sys.stdin)
if len(album["items"]) <= 6 and album["fields"].get("albumtype") != "ep":
    print(json.dumps({"rationale": "6 tracks or fewer",
                      "changes": [{"field": "albumtype", "new": "ep"}]}))
"""


# -- SHELL-11 ---------------------------------------------------------------------------


def test_SHELL_11_emits_accepts_none_and_changes() -> None:
    assert errors(step({"run": ["ls"], "emits": "changes"})) == []
    assert errors(step({"run": ["ls"], "emits": "none"})) == []
    assert errors(step({"run": ["ls"], "emits": "json"})) != []


# -- SHELL-14: stdout is the proposal --------------------------------------------------


async def test_SHELL_14_stdout_is_not_logged_but_stderr_is(sh: ShellHarness) -> None:
    sh.album("A")
    code = "import sys; print('to stderr', file=sys.stderr); print('')"
    run = await sh.run(step(script(code)))
    assert run.state is RunState.succeeded
    assert sh.lines(run, "stdout") == []
    assert sh.lines(run, "stderr") == ["to stderr"]


async def test_SHELL_14_empty_output_means_no_changes(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step(script("print('   ')")))
    assert run.state is RunState.succeeded
    assert proposed(sh, run) == []


async def test_SHELL_14_nonzero_exit_discards_stdout(sh: ShellHarness) -> None:
    sh.album("A")
    proposal = json.dumps({"changes": [{"field": "label", "new": "L"}]})
    run = await sh.run(step(script(f"print({json.dumps(proposal)}); raise SystemExit(3)")))
    assert run.state is RunState.failed
    assert proposed(sh, run) == []
    assert "exited with status 3" in sh.lines(run, "error")[0]


async def test_SHELL_14_output_over_the_limit_fails_the_target(
    sh: ShellHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell, "MAX_PROPOSAL", 100)
    sh.album("A")
    run = await sh.run(step(script("print('x' * 1000)")))
    assert run.state is RunState.failed
    assert "larger than" in sh.lines(run, "error")[0]


# -- SHELL-15: short form ---------------------------------------------------------------


async def test_SHELL_15_short_form_targets_the_current_album(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t1", "track": 1}])
    sh.album("B", items=[{"title": "t2", "track": 1}])
    run = await sh.run(step(printing({"changes": [{"field": "label", "new": "L"}]})))
    assert run.state is RunState.waiting_review
    sets = proposed(sh, run)
    assert [cs.targets[0].target.id for cs in sets] == [album_id(sh, "A"), album_id(sh, "B")]
    assert all(cs.targets[0].target.scope == "album" for cs in sets)


async def test_SHELL_15_short_form_with_per_item_targets_the_item(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t1", "track": 1}, {"title": "t2", "track": 2}])
    run = await sh.run(step(printing({"changes": [{"field": "mood", "new": "calm"}]}, per="item")))
    sets = proposed(sh, run)  # one invocation, so one proposal, per item
    assert [[t.target.scope for t in cs.targets] for cs in sets] == [["item"], ["item"]]
    items = [i.id for i in sh.backend.query(Selection())[0].items]
    assert [cs.targets[0].target.id for cs in sets] == items


async def test_SHELL_15_per_run_needs_targets(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step(printing({"changes": [{"field": "label", "new": "L"}]}, per="run")))
    assert run.state is RunState.failed
    assert "per: run needs 'targets'" in str(run.steps[0].error)


async def test_SHELL_15_both_forms_are_invalid(sh: ShellHarness) -> None:
    sh.album("A")
    aid = album_id(sh, "A")
    both = {
        "changes": [{"field": "label", "new": "L"}],
        "targets": [
            {"target": {"scope": "album", "id": aid}, "changes": [{"field": "label", "new": "L"}]}
        ],
    }
    run = await sh.run(step(printing(both)))
    assert run.state is RunState.failed
    assert "exactly one of 'targets' or 'changes'" in sh.lines(run, "error")[0]


# -- SHELL-16: scope --------------------------------------------------------------------


def _target(scope: str, id: str, **change: Any) -> dict[str, Any]:
    return {"target": {"scope": scope, "id": id}, "changes": [change]}


async def test_SHELL_16_per_album_may_not_touch_another_album(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    other = album_id(sh, "B")
    code = f"""
import json, sys
album = json.load(sys.stdin)
targets = [{{"target": {{"scope": "album", "id": album["id"]}},
            "changes": [{{"field": "label", "new": "mine"}}]}}]
if album["fields"]["album"] == "A":
    targets.append({{"target": {{"scope": "album", "id": "{other}"}},
                    "changes": [{{"field": "label", "new": "theirs"}}]}})
print(json.dumps({{"targets": targets}}))
"""
    run = await sh.run(step(script(code), extra="    on_error: continue\n"))
    errors_ = sh.lines(run, "error")
    assert len(errors_) == 1
    assert "$.targets[1].target" in errors_[0]
    assert f"album {other} is outside this invocation" in errors_[0]
    sets = proposed(sh, run)  # nothing from A's invocation; B's own change only
    assert [cs.targets[0].target.id for cs in sets] == [other]


async def test_SHELL_16_per_item_may_change_only_its_item(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t1", "track": 1}])
    aid = album_id(sh, "A")
    run = await sh.run(
        step(printing({"targets": [_target("album", aid, field="label", new="L")]}, per="item"))
    )
    assert run.state is RunState.failed
    assert "per: item may change its item only" in sh.lines(run, "error")[0]
    assert proposed(sh, run) == []


async def test_SHELL_16_per_run_may_change_every_selected_album(sh: ShellHarness) -> None:
    sh.album("A", items=[{"title": "t1", "track": 1}])
    sh.album("B")
    a, b = album_id(sh, "A"), album_id(sh, "B")
    item = sh.backend.query(Selection())[0].items[0].id
    targets = [
        _target("album", a, field="label", new="L"),
        _target("item", item, field="mood", new="calm"),
        _target("album", b, field="label", new="L"),
    ]
    run = await sh.run(step(printing({"targets": targets}, per="run")))
    assert run.state is RunState.waiting_review
    assert len(proposed(sh, run)) == 2  # split per album
    assert info(sh, run) == ["run: proposed 2 ChangeSets, 3 field changes"]


# -- SHELL-17: bad output ---------------------------------------------------------------


async def test_SHELL_17_invalid_json_fails_the_target(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(step(script("print('{not json')")))
    assert run.state is RunState.failed
    assert "stdout is not valid JSON" in sh.lines(run, "error")[0]
    assert "line 1" in sh.lines(run, "error")[0]


async def test_SHELL_17_schema_errors_name_the_json_path(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(
        step(printing({"changes": [{"field": "label", "new": "L"}, {"field": "Bad"}]}))
    )
    error = sh.lines(run, "error")[0]
    assert "$.changes[1].field" in error
    assert proposed(sh, run) == []


async def test_SHELL_17_backend_check_problems_name_the_json_path(sh: ShellHarness) -> None:
    sh.album("A", year=2001)
    run = await sh.run(
        step(
            printing(
                {"changes": [{"field": "label", "new": "L"}, {"field": "year", "new": "soon"}]}
            )
        )
    )
    error = sh.lines(run, "error")[0]
    assert "$.changes[1].year" in error
    assert "expects int" in error
    assert proposed(sh, run) == []


async def test_SHELL_17_failures_follow_on_error(sh: ShellHarness) -> None:
    sh.album("A")
    sh.album("B")
    code = """
import json, sys
album = json.load(sys.stdin)
print("{oops" if album["fields"]["album"] == "A" else json.dumps(
    {"changes": [{"field": "label", "new": "L"}]}))
"""
    run = await sh.run(step(script(code), extra="    on_error: continue\n"))
    assert len(sh.lines(run, "error")) == 1
    assert len(proposed(sh, run)) == 1
    assert run.steps[0].failed_targets[0].target == album_id(sh, "A")


# -- SHELL-18: through ctx.propose ------------------------------------------------------


async def test_SHELL_18_old_comes_from_the_snapshot_and_source_is_shell(sh: ShellHarness) -> None:
    sh.album("A", label="Old")
    run = await sh.run(step(printing({"changes": [{"field": "label", "new": "New"}]})))
    (cs,) = proposed(sh, run)
    (change,) = cs.targets[0].changes
    assert (change.old, change.new) == ("Old", "New")
    assert (cs.source.kind, cs.source.name, cs.step_id) == ("step", "shell", "s")


async def test_SHELL_18_apply_auto_applies_right_away(sh: ShellHarness) -> None:
    sh.album("A")
    run = await sh.run(
        step(printing({"changes": [{"field": "label", "new": "L"}]}), extra="    apply: auto\n")
    )
    assert run.state is RunState.succeeded
    assert sh.backend.query(Selection())[0].fields["label"] == "L"


async def test_SHELL_18_spec_example_ep_type(sh: ShellHarness) -> None:
    sh.album("Live", albumtype="album", items=[{"title": f"t{n}", "track": n} for n in range(5)])
    run = await sh.run(step(script(EP_TYPE)))
    assert run.state is RunState.waiting_review
    (cs,) = proposed(sh, run)
    assert cs.rationale == "6 tracks or fewer"
    (change,) = cs.targets[0].changes
    assert (change.field, change.old, change.new) == ("albumtype", "album", "ep")
    assert info(sh, run) == [f"{album_id(sh, 'Live')}: proposed 1 ChangeSet, 1 field change"]


# -- SHELL-19: summary lines ------------------------------------------------------------


async def test_SHELL_19_one_summary_line_per_target(sh: ShellHarness) -> None:
    sh.album("A", label="L")
    sh.album("B")
    run = await sh.run(step(printing({"changes": [{"field": "label", "new": "L"}]})))
    a, b = album_id(sh, "A"), album_id(sh, "B")
    # A already has the value, so its change is a no-op (CHANGESET-4).
    assert info(sh, run) == [f"{a}: no changes", f"{b}: proposed 1 ChangeSet, 1 field change"]
