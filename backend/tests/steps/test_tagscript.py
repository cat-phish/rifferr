"""S-TAGSCRIPT: the ``tags.script`` step (TAGSCRIPT-1, -14 .. -18)."""

from __future__ import annotations

import hashlib
import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow, safety_report
from rifferr.engine import Engine, InMemoryEventSink, Run, RunState, StepTypeRegistry
from rifferr.steps.tagscript import TagScriptStep
from rifferr.tagscript.evaluator import run_script
from rifferr.tagscript.models import TagScript
from tests.engine.conftest import FixedClock

RUN_ID = "01RUN000000000000000000001"
SLASH = chr(0x2215)  # DIVISION SLASH, a filename-safe "/"

EP_SCRIPT = """\
# illustrative, from the spec's Examples
scope: album
actions:
  - name: ep-suffix
    when: 'albumtype:ep ^album::[(]EP[)]$'
    set: { album: '$album (EP)' }
  - replace: { fields: [title], find: '/', with: '<slash>' }
    scope: item
""".replace("<slash>", SLASH)


def yaml_load(text: str) -> Any:
    from rifferr.core.workflow.yamldoc import parse

    return parse(text, "s.yaml").plain()


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.sink = InMemoryEventSink()
        ids = iter(f"01RUN{n:021d}" for n in range(1, 1000))
        self.engine = Engine(
            StepTypeRegistry([TagScriptStep()]),
            self.sink,
            clock=FixedClock(),
            new_id=lambda: next(ids),
        )
        self.backend = FakeBackend()
        self.profile = tmp_path / "profile"
        (self.profile / "scripts").mkdir(parents=True)

    def album(self, name: str, *titles: str, **fields: Any) -> str:
        items = [{"title": t, "track": n + 1} for n, t in enumerate(titles)]
        spec = {"album": name, "albumartist": "Band", RUN_MARKER: RUN_ID, **fields}
        return self.backend.add_album({**spec, "items": items}).id

    def script(self, name: str, text: str) -> Path:
        path = self.profile / "scripts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
        return path

    async def run(
        self, src: str, *, vars: dict[str, str] | None = None, trusted: bool = False
    ) -> Run:
        text = textwrap.dedent(src)
        wf, doc = load_workflow(text, "wf.yaml")
        run = self.engine.create_run(
            wf,
            text.encode(),
            profile_name="main",
            profile_vars=vars or {},
            backend=self.backend,
            doc=doc,
            trusted=trusted,
            work_dir=self.profile,
            profile_dir=self.profile,
        )
        await self.engine.execute(run)
        return run

    def changesets(self, run: Run) -> list[Any]:
        return [s.changeset for s in self.engine.changesets.for_run(run.id)]

    def logs(self, run: Run, stream: str | None = None) -> list[str]:
        return [
            str(e.data["line"])
            for e in self.sink.since(run.id, 0)
            if e.type == "run.step.log" and (stream is None or e.data["stream"] == stream)
        ]

    def fields(self, album_id: str) -> dict[str, Any]:
        return next(a for a in self.backend.query(Selection()) if a.id == album_id).fields

    def titles(self, album_id: str) -> list[Any]:
        album = next(a for a in self.backend.query(Selection()) if a.id == album_id)
        return [i.fields["title"] for i in album.items]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def workflow(with_: str, *, apply: str = "review", extra: str = "") -> str:
    return f"""\
name: tags
steps:
  - id: release-types
    uses: tags.script
    apply: {apply}
{extra}    with:
{textwrap.indent(textwrap.dedent(with_), "      ")}
"""


# -- TAGSCRIPT-1: scripts -------------------------------------------------------------


async def test_TAGSCRIPT_1_script_file_from_profile_scripts_folder(h: Harness) -> None:
    album = h.album("Live Sessions", "A/B", albumtype="ep")
    h.script("ep-suffix.yaml", EP_SCRIPT)
    run = await h.run(workflow("script: ep-suffix.yaml"))
    assert run.state is RunState.waiting_review
    (cs,) = h.changesets(run)
    assert cs.targets[0].target.id == album


async def test_TAGSCRIPT_1_interpolation_in_script_file(h: Harness) -> None:
    album = h.album("Live", "x")
    h.script("s.yaml", "actions:\n  - set: {albumdisambig: '{{ vars.note }} $album'}\n")
    run = await h.run(workflow("script: s.yaml"), vars={"note": "from vars"})
    (cs,) = h.changesets(run)
    (change,) = cs.targets[0].changes
    assert cs.targets[0].target.id == album
    assert change.new == "from vars Live"


async def test_TAGSCRIPT_1_symlink_leaving_scripts_folder_fails(h: Harness, tmp_path: Path) -> None:
    h.album("A", "x")
    outside = tmp_path / "outside.yaml"
    outside.write_text("actions:\n  - delete: [label]\n")
    os.symlink(outside, h.profile / "scripts" / "link.yaml")
    run = await h.run(workflow("script: link.yaml"))
    assert run.state is RunState.failed
    assert "leaves the profile's scripts/ folder" in (run.steps[0].error or "")


async def test_TAGSCRIPT_1_missing_and_invalid_scripts_fail(h: Harness) -> None:
    h.album("A", "x")
    run = await h.run(workflow("script: nope.yaml"))
    assert "not found" in (run.steps[0].error or "")
    h.script("bad.yaml", "actions:\n  - sett: {a: 1}\n")
    run = await h.run(workflow("script: bad.yaml"))
    assert run.state is RunState.failed
    assert "bad.yaml:2:5: actions[0]" in (run.steps[0].error or "")


def test_TAGSCRIPT_1_absolute_script_path_is_a_validation_error() -> None:
    from rifferr.core.workflow import validate
    from rifferr.steps.registry import load_step_types

    wf, doc = load_workflow(workflow("script: /etc/passwd"), "wf.yaml")
    errors = validate(wf, load_step_types(), doc=doc)
    assert any("relative to the profile's scripts/ folder" in e.message for e in errors)


async def test_TAGSCRIPT_1_step_parameters_override_the_file(h: Harness) -> None:
    h.album("Live Sessions", "x", albumtype="ep")
    h.script("loose.yaml", "idempotence: off\nactions:\n  - set: {album: '$album (EP)'}\n")
    run = await h.run(workflow("script: loose.yaml\nidempotence: fail", extra=""))
    assert run.state is RunState.failed
    assert h.changesets(run) == []


# -- TAGSCRIPT-14: net changes as ChangeSets ------------------------------------------


async def test_TAGSCRIPT_14_spec_example_proposes_one_changeset(h: Harness) -> None:
    album = h.album("Live Sessions", "Intro", "A/B", albumtype="ep")
    h.script("ep-suffix.yaml", EP_SCRIPT)
    run = await h.run(workflow("script: ep-suffix.yaml"))
    (cs,) = h.changesets(run)
    flat = [(t.id, c.field, c.old, c.new, c.rationale) for t, c in cs.field_changes()]
    item_ab = next(t.id for t, c in cs.field_changes() if t.scope == "item")
    assert flat == [
        (album, "album", "Live Sessions", "Live Sessions (EP)", "ep-suffix"),
        (item_ab, "title", "A/B", f"A{SLASH}B", "action 2"),
    ]
    assert cs.rationale == "tags.script ep-suffix.yaml"
    assert cs.source.name == "tags.script"


async def test_TAGSCRIPT_14_apply_then_rerun_proposes_nothing(h: Harness) -> None:
    album = h.album("Live Sessions", "A/B", albumtype="ep")
    h.script("ep-suffix.yaml", EP_SCRIPT)
    run = await h.run(workflow("script: ep-suffix.yaml", apply="auto"))
    assert run.state is RunState.succeeded
    assert h.fields(album)["album"] == "Live Sessions (EP)"
    assert h.titles(album) == [f"A{SLASH}B"]
    # a later run over the same album finds nothing left to do
    (snapshot,) = h.backend.query(Selection())
    loaded = TagScript.model_validate(yaml_load(EP_SCRIPT))
    assert run_script(loaded, snapshot, h.backend).diffs() == []


async def test_TAGSCRIPT_14_inline_actions_and_deletes(h: Harness) -> None:
    album = h.album("A", "x", label="L", comments="c")
    run = await h.run(
        workflow(
            """\
            actions:
              - delete: [label]
              - set: {comments: c}
            """
        )
    )
    (cs,) = h.changesets(run)
    ((target, change),) = cs.field_changes()
    assert (target.id, change.field, change.op, change.old) == (album, "label", "delete", "L")


# -- TAGSCRIPT-15: errors fail the album ------------------------------------------------


async def test_TAGSCRIPT_15_error_fails_the_album_and_proposes_nothing(h: Harness) -> None:
    good = h.album("Good", "x", year=2001)
    h.album("Bad", "x", year=2002, note="soon")
    run = await h.run(
        workflow(
            """\
            actions:
              - set: {albumdisambig: ok}
              - name: year-from-note
                when: 'note:soon'
                set: {year: $note}
            """,
            extra="    on_error: continue\n",
        )
    )
    assert run.steps[0].status == "failed"
    (failed,) = run.steps[0].failed_targets
    assert "year-from-note (#2)" in failed.message
    assert "year" in failed.message
    assert [cs.targets[0].target.id for cs in h.changesets(run)] == [good]


# -- TAGSCRIPT-16: idempotence -------------------------------------------------------------


NO_GUARD = """\
actions:
  - name: ep-suffix
    when: 'albumtype:ep'
    set: { album: '$album (EP)' }
"""


async def test_TAGSCRIPT_16_warn_logs_and_still_proposes(h: Harness) -> None:
    h.album("Live Sessions", "x", albumtype="ep")
    h.script("s.yaml", NO_GUARD)
    run = await h.run(workflow("script: s.yaml"))
    (warning,) = h.logs(run, "warning")
    assert "second pass of action 'ep-suffix'" in warning
    assert '"Live Sessions (EP)" to "Live Sessions (EP) (EP)"' in warning
    (cs,) = h.changesets(run)
    assert cs.targets[0].changes[0].new == "Live Sessions (EP)"


async def test_TAGSCRIPT_16_fail_fails_the_album(h: Harness) -> None:
    h.album("Live Sessions", "x", albumtype="ep")
    h.script("s.yaml", "idempotence: fail\n" + NO_GUARD)
    run = await h.run(workflow("script: s.yaml"))
    assert run.state is RunState.failed
    assert h.changesets(run) == []
    assert "not idempotent" in run.steps[0].failed_targets[0].message


async def test_TAGSCRIPT_16_off_skips_the_check(h: Harness) -> None:
    h.album("Live Sessions", "x", albumtype="ep")
    h.script("s.yaml", "idempotence: off\n" + NO_GUARD)
    run = await h.run(workflow("script: s.yaml"))
    assert h.logs(run, "warning") == []
    assert any("idempotence off" in line for line in h.logs(run, "info"))


# -- TAGSCRIPT-17: summary and step record -------------------------------------------


async def test_TAGSCRIPT_17_summary_line_per_album_and_script_record(h: Harness) -> None:
    a = h.album("Live Sessions", "A/B", albumtype="ep")
    b = h.album("Other", "plain")
    path = h.script("ep-suffix.yaml", EP_SCRIPT)
    run = await h.run(workflow("script: ep-suffix.yaml"))
    assert h.logs(run, "info") == [
        f"{a}: 2 field changes proposed; idempotent",
        f"{b}: no changes; idempotent",
    ]
    outputs = run.steps[0].outputs
    assert outputs["script_text"] == path.read_text()
    assert outputs["script_path"] == "ep-suffix.yaml"
    assert outputs["script_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert outputs["changesets"] == 1
    assert outputs["field_changes"] == 2


# -- TAGSCRIPT-18: no trust, safety finding -----------------------------------------------


async def test_TAGSCRIPT_18_runs_untrusted(h: Harness) -> None:
    h.album("A", "x")
    run = await h.run(workflow("actions:\n  - set: {comments: hi}\n"), trusted=False)
    assert run.state is RunState.waiting_review


def test_TAGSCRIPT_18_safety_report_lists_auto_apply() -> None:
    wf, _ = load_workflow(workflow("script: s.yaml", apply="auto"), "wf.yaml")
    (finding,) = safety_report(wf).of_kind("auto_apply")
    assert finding.step_id == "release-types"
    wf, _ = load_workflow(workflow("script: s.yaml"), "wf.yaml")
    assert safety_report(wf).of_kind("auto_apply") == []


# -- TAGSCRIPT-4: item-scoped scripts ----------------------------------------------------


async def test_TAGSCRIPT_4_item_scoped_script(h: Harness) -> None:
    h.album("A", "one", "two")
    run = await h.run(
        workflow(
            """\
            scope: item
            actions:
              - case: {fields: [title], style: title}
            """
        )
    )
    (cs,) = h.changesets(run)
    assert [c.new for _, c in cs.field_changes()] == ["One", "Two"]


def test_TAGSCRIPT_3_backend_shared_fields_are_used(h: Harness) -> None:
    assert "album" in FakeBackend().album_item_fields()
