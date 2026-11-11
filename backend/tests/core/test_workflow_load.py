from __future__ import annotations

import textwrap

import pytest

from rifferr.core.workflow import (
    StepSpec,
    Workflow,
    WorkflowLoadError,
    load_workflow,
)


def load(src: str) -> Workflow:
    return load_workflow(textwrap.dedent(src), "test.yaml")[0]


def errors(src: str) -> list[str]:
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow(textwrap.dedent(src), "test.yaml")
    return [str(e) for e in exc.value.errors]


MINIMAL = """\
name: two-step
description: Import then list
vars:
  inbox: { description: "Folder new downloads land in" }
steps:
  - id: import
    uses: beets.import
    with: { path: "{{ vars.inbox }}" }
  - id: list
    uses: shell
    with:
      run: [ls, $path]
"""


def test_WORKFLOW_1_minimal_workflow_parses() -> None:
    wf = load(MINIMAL)
    assert wf.name == "two-step"
    assert wf.description == "Import then list"
    assert wf.vars["inbox"].description == "Folder new downloads land in"
    assert wf.vars["inbox"].default is None
    assert [s.id for s in wf.steps] == ["import", "list"]
    assert wf.steps[0].with_ == {"path": "{{ vars.inbox }}"}
    assert wf.on is None


def test_WORKFLOW_1_steps_must_be_non_empty() -> None:
    assert any("steps" in e for e in errors("name: x\nsteps: []\n"))


def test_WORKFLOW_1_name_is_required() -> None:
    assert any(
        "name" in e and "required" in e.lower() for e in errors("steps: [{id: a, uses: shell}]\n")
    )


def test_WORKFLOW_1_top_level_must_be_a_mapping() -> None:
    assert errors("- a\n- b\n") != []


def test_WORKFLOW_1_yaml_syntax_error_has_location() -> None:
    errs = errors("name: x\nsteps:\n  - id: a\n    uses: [unclosed\n")
    assert len(errs) == 1
    assert errs[0].startswith("test.yaml:")


def test_WORKFLOW_1_duplicate_yaml_keys_are_errors() -> None:
    errs = errors("name: x\nname: y\nsteps: [{id: a, uses: shell}]\n")
    assert any("duplicate" in e.lower() for e in errs)


@pytest.mark.parametrize("bad", ["Import", "1st", "has space", "", "a.b"])
def test_WORKFLOW_2_step_id_pattern(bad: str) -> None:
    errs = errors(f"name: x\nsteps:\n  - id: '{bad}'\n    uses: shell\n")
    assert any("steps[0].id" in e for e in errs)


@pytest.mark.parametrize("good", ["a", "import", "step_2", "list-files"])
def test_WORKFLOW_2_valid_step_ids(good: str) -> None:
    assert load(f"name: x\nsteps:\n  - id: {good}\n    uses: shell\n").steps[0].id == good


def test_WORKFLOW_2_uses_is_required() -> None:
    assert any("steps[0].uses" in e for e in errors("name: x\nsteps:\n  - id: a\n"))


def test_WORKFLOW_2_optional_step_fields() -> None:
    step = load(
        "name: x\nsteps:\n  - id: a\n    uses: shell\n    name: Say hi\n    when: 'albumtype:ep'\n"
    ).steps[0]
    assert step.name == "Say hi"
    assert step.when == "albumtype:ep"
    assert step.with_ == {}


def test_WORKFLOW_3_defaults() -> None:
    step = load("name: x\nsteps: [{id: a, uses: shell}]\n").steps[0]
    assert step.apply == "review"
    assert step.on_error == "stop"


@pytest.mark.parametrize(("field", "value"), [("apply", "always"), ("on_error", "retry")])
def test_WORKFLOW_3_invalid_policy_values(field: str, value: str) -> None:
    errs = errors(f"name: x\nsteps:\n  - id: a\n    uses: shell\n    {field}: {value}\n")
    assert any(f"steps[0].{field}" in e for e in errs)


def test_WORKFLOW_3_valid_policy_values() -> None:
    step = load(
        "name: x\nsteps:\n  - {id: a, uses: shell, apply: auto, on_error: continue}\n"
    ).steps[0]
    assert (step.apply, step.on_error) == ("auto", "continue")


def test_WORKFLOW_4_unknown_top_level_key_names_key_and_location() -> None:
    errs = errors("name: x\nstpes: []\nsteps: [{id: a, uses: shell}]\n")
    assert errs == ["test.yaml:2:1: stpes: unknown key"]


def test_WORKFLOW_4_unknown_step_key_names_key_and_location() -> None:
    errs = errors("name: x\nsteps:\n  - id: a\n    uses: shell\n    retries: 3\n")
    assert errs == ["test.yaml:5:5: steps[0].retries: unknown key"]


def test_WORKFLOW_4_error_location_for_wrong_type() -> None:
    errs = errors("name: x\nsteps:\n  - id: a\n    uses: shell\n    apply: [auto]\n")
    assert len(errs) == 1
    assert errs[0].startswith("test.yaml:5:12: steps[0].apply:")


def test_WORKFLOW_1_model_round_trips_with_alias() -> None:
    step = StepSpec.model_validate({"id": "a", "uses": "shell", "with": {"run": ["ls"]}})
    assert step.model_dump(by_alias=True, exclude_defaults=True) == {
        "id": "a",
        "uses": "shell",
        "with": {"run": ["ls"]},
    }
