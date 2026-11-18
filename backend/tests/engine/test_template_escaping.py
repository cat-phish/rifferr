"""SEC-22: values from outside the workflow stay literal in backend templates.

``{{ }}`` substitutes first; a parameter the backend then evaluates as a template gets
run fields, run-supplied vars and step outputs escaped, so the two evaluators never
read each other's syntax.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow
from rifferr.engine import Run, StepTypeRegistry
from rifferr.engine.review import InMemoryGateStore
from rifferr.steps.notify import NotifyStep
from rifferr.steps.review import ReviewStep
from rifferr.steps.tagscript import TagScriptStep
from tests.engine.conftest import Harness
from tests.steps.test_notify import FakeTargets

LITERAL = "/inbox/$genre %upper{x} a, b}"


def _harness() -> Harness:
    h = Harness()
    h.engine.registry = StepTypeRegistry([ReviewStep(), TagScriptStep(), NotifyStep()])
    return h


async def _run(
    h: Harness,
    steps: str,
    *,
    run_input: str = LITERAL,
    profile_vars: dict[str, str] | None = None,
    run_vars: dict[str, str] | None = None,
    profile_dir: Path | None = None,
    notify: FakeTargets | None = None,
) -> Run:
    text = textwrap.dedent(steps)
    wf, doc = load_workflow(text, "wf.yaml")
    run = h.engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars=profile_vars or {},
        backend=h.backend,
        run_vars=run_vars,
        run_input=run_input,
        doc=doc,
        profile_dir=profile_dir,
        notify=notify,
    )
    h.backend.add_album(
        {
            "albumartist": "Artist",
            "album": "Name",
            "genre": "SECRETGENRE",
            RUN_MARKER: run.id,
            "items": [{"title": "t", "track": 1}],
        }
    )
    await h.engine.execute(run)
    return run


def _messages(h: Harness) -> list[str | None]:
    store = h.engine.gates
    assert isinstance(store, InMemoryGateStore)
    return [a.message for g in store.items.values() for a in g.albums]


def _proposed(h: Harness, run: Run) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for stored in h.engine.changesets.for_run(run.id):
        for t in stored.changeset.targets:
            for c in t.changes:
                out[c.field] = c.new
    return out


REVIEW = """\
name: t
vars: {msg: {default: "$album"}, x: {default: safe}}
steps:
  - id: check
    uses: review
    with: {message: "MESSAGE"}
"""


async def test_SEC_22_run_input_is_literal_in_review_message() -> None:
    h = _harness()
    await _run(h, REVIEW.replace("MESSAGE", "{{ run.input }}"))
    assert _messages(h) == [LITERAL]


async def test_SEC_22_run_supplied_var_is_literal_in_review_message() -> None:
    h = _harness()
    await _run(h, REVIEW.replace("MESSAGE", "{{ vars.x }}"), run_vars={"x": "$genre"})
    assert _messages(h) == ["$genre"]


async def test_SEC_22_workflow_var_stays_a_template() -> None:
    h = _harness()
    await _run(h, REVIEW.replace("MESSAGE", "{{ vars.msg }} by $albumartist"))
    assert _messages(h) == ["Name by Artist"]


async def test_SEC_22_run_input_is_literal_in_inline_tag_script() -> None:
    h = _harness()
    run = await _run(
        h,
        """\
        name: t
        steps:
          - id: tag
            uses: tags.script
            with:
              actions:
                - set: {source: "{{ run.input }}", origin: "$albumartist"}
                - append_unique: {field: genres, values: ["{{ run.input }}"]}
        """,
    )
    proposed = _proposed(h, run)
    assert proposed["source"] == LITERAL
    assert proposed["origin"] == "Artist"
    assert LITERAL in proposed["genres"]


async def test_SEC_22_run_input_is_literal_in_tag_script_file(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "s.yaml").write_text(
        "actions:\n  - set: {source: '{{ run.input }}'}\n", encoding="utf-8"
    )
    h = _harness()
    run = await _run(
        h,
        """\
        name: t
        steps:
          - {id: tag, uses: tags.script, with: {script: s.yaml}}
        """,
        profile_dir=tmp_path,
    )
    assert _proposed(h, run)["source"] == LITERAL


async def test_SEC_22_run_level_notify_is_not_escaped() -> None:
    h = _harness()
    targets = FakeTargets(["t"])
    await _run(
        h,
        """\
        name: t
        steps:
          - {id: tell, uses: notify, with: {body: "{{ run.input }}", title: x, per: run}}
        """,
        notify=targets,
    )
    assert [n.body for _, n in targets.sent] == [LITERAL]


@settings(max_examples=40, deadline=None)
@given(st.text(min_size=1))
async def test_SEC_22_any_run_input_comes_out_unchanged(text: str) -> None:
    h = _harness()
    await _run(h, REVIEW.replace("MESSAGE", "{{ run.input }}"), run_input=text)
    assert _messages(h) == [text]


@given(st.text())
def test_SEC_22_fake_backend_escape_round_trips(text: str) -> None:
    backend = FakeBackend.from_fixture({"albums": [{"album": "A", "items": [{"title": "t"}]}]})
    album = backend.query(Selection())[0]
    assert backend.evaluate(backend.escape_template(text), album) == text
