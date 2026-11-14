"""S-SUBWORKFLOW in core/workflow: call paths, vars, cycles, expansion, outputs."""

from __future__ import annotations

import textwrap

import pytest

from rifferr.backends.base import BackendCapabilities
from rifferr.core.workflow import (
    CalledStep,
    Calls,
    StaticStepRegistry,
    StepSpec,
    StepTypeInfo,
    WorkflowCallParams,
    expand,
    load_workflow,
    resolve_calls,
    stored_reader,
    validate,
)
from rifferr.core.workflow.calls import path_problem
from tests.core.conftest import IMPORT, SHELL

CALL = StepTypeInfo(
    name="workflow", params_model=WorkflowCallParams, requires=BackendCapabilities()
)
REGISTRY = StaticStepRegistry([SHELL, IMPORT, CALL])

TAGGING = """\
name: Tagging
vars:
  case_style: {description: title or sentence}
  rules: {default: rules/genres.yaml}
outputs:
  changed: "{{ steps.fix-case.code }}"
steps:
  - id: fix-case
    uses: shell
    with: {run: [fix, "{{ vars.case_style }}", "{{ vars.rules }}"]}
    when: "genre:Rock"
  - id: genres
    uses: shell
    with: {run: [genres, "{{ steps.fix-case.code }}", "{{ run.input }}"]}
"""

INBOX = """\
name: Inbox
steps:
  - id: import
    uses: beets.import
    with: {path: /in}
  - id: tagging
    uses: workflow
    when: "albumartist:X"
    with:
      workflow: tagging.yaml
      vars: {case_style: "{{ steps.import.count }}"}
  - id: tell
    uses: shell
    with: {run: [echo, "{{ steps.tagging.changed }}"]}
"""


def files(**texts: str) -> dict[str, str]:
    return {k.replace("_", "-") + ".yaml": textwrap.dedent(v) for k, v in texts.items()}


def check(caller: str, called: dict[str, str], name: str | None = "inbox.yaml") -> list[str]:
    wf, doc = load_workflow(textwrap.dedent(caller), name or "wf.yaml")
    calls = resolve_calls(wf, doc, stored_reader(called), name=name)
    return [str(e) for e in validate(wf, REGISTRY, None, {}, doc=doc, calls=calls)]


def expanded(caller: str, called: dict[str, str]) -> list[StepSpec]:
    wf, doc = load_workflow(textwrap.dedent(caller), "inbox.yaml")
    calls = resolve_calls(wf, doc, stored_reader(called), name="inbox.yaml")
    assert validate(wf, REGISTRY, None, {}, doc=doc, calls=calls) == []
    return list(expand(wf, calls).steps)


def call(with_: str, extra: str = "") -> str:
    return f"name: c\nsteps:\n  - {{id: t, uses: workflow, with: {with_}{extra}}}\n"


# -- SUBWORKFLOW-1 ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/srv/x.yaml", "must be relative to the profile's workflows/ folder"),
        ("../other/x.yaml", "workflow '../other/x.yaml' leaves the profile's workflows/ folder"),
        ("a/../../x.yaml", "leaves the profile's workflows/ folder"),
        ("{{ vars.wf }}", "must be a fixed path"),
    ],
)
def test_SUBWORKFLOW_1_bad_paths(path: str, message: str) -> None:
    problem = path_problem(path)
    assert problem is not None
    assert message in problem
    errors = check(call(f"{{workflow: '{path}'}}"), {})
    assert any(message in e for e in errors), errors


def test_SUBWORKFLOW_1_subfolders_inside_workflows_are_fine() -> None:
    assert path_problem("shared/tagging.yaml") is None
    assert path_problem("a/../tagging.yaml") is None


def test_SUBWORKFLOW_1_missing_and_invalid_called_files() -> None:
    errors = check(call("{workflow: nope.yaml}"), {})
    assert errors == [
        "inbox.yaml:3:46: steps[0].with.workflow: "
        "workflow 'nope.yaml' not found in the profile's workflows/ folder"
    ]
    errors = check(call("{workflow: bad.yaml}"), {"bad.yaml": "name: bad\nsteps: []\n"})
    assert len(errors) == 1
    assert errors[0].startswith("bad.yaml:")


def test_SUBWORKFLOW_1_called_files_are_validated_in_their_own_file() -> None:
    bad = "name: b\nsteps:\n  - {id: x, uses: nosuch}\n"
    (error,) = check(call("{workflow: b.yaml}"), {"b.yaml": bad})
    assert error.startswith("b.yaml:3:")
    assert "unknown step type 'nosuch'" in error


# -- SUBWORKFLOW-2 ------------------------------------------------------------------------


def test_SUBWORKFLOW_2_undeclared_and_missing_vars() -> None:
    errors = check(call("{workflow: tagging.yaml, vars: {case: title}}"), {"tagging.yaml": TAGGING})
    assert any("tagging.yaml declares no var 'case'" in e for e in errors)
    assert any("tagging.yaml needs var 'case_style'" in e for e in errors)


def test_SUBWORKFLOW_2_profile_vars_are_not_passed_implicitly() -> None:
    called = "name: c\nsteps:\n  - {id: x, uses: shell, with: {run: ['{{ vars.inbox }}']}}\n"
    wf, doc = load_workflow(call("{workflow: c.yaml}"), "inbox.yaml")
    calls = resolve_calls(wf, doc, stored_reader({"c.yaml": called}), name="inbox.yaml")
    errors = validate(wf, REGISTRY, None, {"inbox": "/in"}, doc=doc, calls=calls)
    assert [(e.source, e.message) for e in errors] == [
        ("c.yaml", "variable 'inbox' is not declared in vars (a called workflow gets only its own)")
    ]


# -- SUBWORKFLOW-4, -11 (validation) ----------------------------------------------------------


def test_SUBWORKFLOW_4_called_steps_refer_to_their_own_steps_only() -> None:
    called = (
        "name: c\nsteps:\n  - {id: x, uses: shell, with: {run: ['{{ steps.import.count }}']}}\n"
    )
    caller = (
        "name: i\nsteps:\n  - {id: import, uses: beets.import, with: {path: /in}}\n"
        "  - {id: t, uses: workflow, with: {workflow: c.yaml}}\n"
    )
    (error,) = check(caller, {"c.yaml": called})
    assert error.startswith("c.yaml:")
    assert "must refer to an earlier step" in error


def test_SUBWORKFLOW_11_reading_an_undeclared_output_is_an_error() -> None:
    caller = INBOX.replace("steps.tagging.changed", "steps.tagging.nope")
    (error,) = check(caller, {"tagging.yaml": TAGGING})
    assert "'{{ steps.tagging.nope }}': tagging.yaml declares no output 'nope'" in error


def test_SUBWORKFLOW_11_outputs_must_name_own_steps() -> None:
    called = TAGGING.replace("steps.fix-case.code }}", "steps.other.code }}", 1)
    errors = check(INBOX, {"tagging.yaml": called})
    assert any("no step 'other' in this workflow" in e for e in errors), errors
    called = TAGGING.replace("{{ steps.fix-case.code }}", "{{ vars.rules }}", 1)
    errors = check(INBOX, {"tagging.yaml": called})
    assert any("outputs may only use steps.<id>.<key>" in e for e in errors), errors


def test_SUBWORKFLOW_11_a_workflow_on_its_own_ignores_outputs() -> None:
    wf, doc = load_workflow(textwrap.dedent(TAGGING), "tagging.yaml")
    assert validate(wf, REGISTRY, None, {"case_style": "title"}, doc=doc) == []


# -- SUBWORKFLOW-5 ------------------------------------------------------------------------


def test_SUBWORKFLOW_5_cycle_is_an_error_naming_it() -> None:
    tagging = call("{workflow: inbox.yaml}")
    errors = check(call("{workflow: tagging.yaml}"), {"tagging.yaml": tagging})
    assert errors == [
        "tagging.yaml:3:46: steps[0].with.workflow: "
        "workflow call cycle: inbox.yaml → tagging.yaml → inbox.yaml"
    ]
    assert any(
        "cycle: a.yaml → a.yaml" in e
        for e in check(
            call("{workflow: a.yaml}"), {"a.yaml": call("{workflow: a.yaml}")}, name=None
        )
    )


def chain(n: int) -> dict[str, str]:
    """w1 calls w2 ... calls wn; wn is a plain workflow."""
    out = {f"w{i}.yaml": call(f"{{workflow: w{i + 1}.yaml}}") for i in range(1, n)}
    out[f"w{n}.yaml"] = "name: end\nsteps:\n  - {id: x, uses: shell, with: {run: [ls]}}\n"
    return out


def test_SUBWORKFLOW_5_calls_nest_at_most_8_deep() -> None:
    assert check(call("{workflow: w1.yaml}"), chain(8)) == []
    errors = check(call("{workflow: w1.yaml}"), chain(9))
    assert len(errors) == 1
    assert "workflow calls nest more than 8 deep" in errors[0]


# -- SUBWORKFLOW-6, -7, -8, -10 --------------------------------------------------------------


def test_SUBWORKFLOW_6_calls_expand_in_place_with_joined_ids() -> None:
    steps = expanded(INBOX, {"tagging.yaml": TAGGING})
    assert [s.id for s in steps] == [
        "import",
        "tagging/fix-case",
        "tagging/genres",
        "tagging",
        "tell",
    ]
    fix, genres, end = steps[1], steps[2], steps[3]
    assert isinstance(fix, CalledStep)
    assert (fix.call_path, fix.workflow) == (("tagging",), "tagging.yaml")
    # SUBWORKFLOW-2, -3: vars become the caller's expression or the default.
    assert fix.with_ == {"run": ["fix", "{{ steps.import.count }}", "rules/genres.yaml"]}
    # SUBWORKFLOW-4: own steps get the expanded id; run stays the caller's.
    assert isinstance(genres, CalledStep)
    assert genres.with_["run"] == ["genres", "{{ steps.tagging/fix-case.code }}", "{{ run.input }}"]
    assert isinstance(end, CalledStep)
    assert (end.uses, end.outputs) == ("workflow", {"changed": "{{ steps.tagging/fix-case.code }}"})


def test_SUBWORKFLOW_6_nested_calls_and_reserved_slash() -> None:
    post = (
        "name: p\nsteps:\n  - {id: tagging, uses: workflow,"
        " with: {workflow: tagging.yaml, vars: {case_style: s}}}\n"
    )
    caller = "name: i\nsteps:\n  - {id: post, uses: workflow, with: {workflow: post.yaml}}\n"
    steps = expanded(caller, {"post.yaml": post, "tagging.yaml": TAGGING})
    assert [s.id for s in steps] == ["post/tagging/fix-case", "post/tagging/genres", "post/tagging"]
    with pytest.raises(Exception, match="pattern"):
        load_workflow("name: x\nsteps:\n  - {id: a/b, uses: shell}\n")


def test_SUBWORKFLOW_7_call_when_narrows_called_steps() -> None:
    nested = (
        "name: p\nsteps:\n  - {id: t, uses: workflow, when: 'year:2000',"
        " with: {workflow: tagging.yaml, vars: {case_style: s}}}\n"
    )
    caller = (
        "name: i\nsteps:\n  - {id: p, uses: workflow, when: 'albumartist:X',"
        " with: {workflow: p.yaml}}\n"
    )
    fix = expanded(caller, {"p.yaml": nested, "tagging.yaml": TAGGING})[0]
    assert isinstance(fix, CalledStep)
    assert (fix.when, fix.narrow) == ("genre:Rock", ("albumartist:X", "year:2000"))


def test_SUBWORKFLOW_8_apply_and_on_error_on_a_call_are_errors() -> None:
    errors = check(
        call("{workflow: c.yaml}", ", apply: auto, on_error: skip"), {"c.yaml": chain(1)["w1.yaml"]}
    )
    assert [e.split(": ", 2)[2] for e in errors] == [
        "'apply' is not allowed on a workflow call; each called step's own value applies",
        "'on_error' is not allowed on a workflow call; each called step's own value applies",
    ]


def test_SUBWORKFLOW_10_called_on_and_input_are_ignored() -> None:
    called = (
        "name: c\ninput: '{{ steps.x.y }}'\non: {watch: {path: '{{ run.input }}'}}\n"
        "steps:\n  - {id: x, uses: shell, with: {run: [ls]}}\n"
    )
    assert check(call("{workflow: c.yaml}"), {"c.yaml": called}) == []
    wf, doc = load_workflow(call("{workflow: c.yaml}"), "inbox.yaml")
    calls = resolve_calls(wf, doc, stored_reader({"c.yaml": called}))
    out = expand(wf, calls)
    assert (out.on, out.input, out.name) == (None, None, "c")


def test_SUBWORKFLOW_12_calls_keep_the_texts_read() -> None:
    wf, doc = load_workflow(textwrap.dedent(INBOX), "inbox.yaml")
    texts = files(tagging=TAGGING)
    calls = resolve_calls(wf, doc, stored_reader(texts))
    assert isinstance(calls, Calls)
    assert calls.texts() == texts
