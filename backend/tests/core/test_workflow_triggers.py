from __future__ import annotations

import pytest

from rifferr.core.workflow import WorkflowLoadError, load_workflow


def test_WORKFLOW_13_on_block_parses_all_triggers() -> None:
    wf, _ = load_workflow(
        "name: x\non:\n  watch: { path: '{{ vars.inbox }}', settle: 10m }\n"
        "  schedule: '0 4 * * *'\n  webhook: { name: hook, input: a.path }\n"
        "steps: [{id: a, uses: shell}]\n",
        "wf.yaml",
    )
    assert wf.on is not None
    assert wf.on.watch is not None
    assert wf.on.watch.path == "{{ vars.inbox }}"
    assert wf.on.watch.settle == "10m"
    assert wf.on.schedule == "0 4 * * *"
    assert wf.on.webhook is not None
    assert wf.on.webhook.name == "hook"
    assert wf.on.webhook.input == "a.path"


def test_WORKFLOW_13_on_is_a_key_not_a_yaml_boolean() -> None:
    wf, _ = load_workflow(
        "name: x\non: { schedule: '@daily' }\nsteps: [{id: a, uses: shell}]\n", "wf.yaml"
    )
    assert wf.on is not None


def test_WORKFLOW_13_unknown_trigger_is_an_error() -> None:
    with pytest.raises(WorkflowLoadError) as exc:
        load_workflow("name: x\non: { cron: x }\nsteps: [{id: a, uses: shell}]\n", "wf.yaml")
    assert [str(e) for e in exc.value.errors] == ["wf.yaml:2:7: on.cron: unknown key"]


def test_WORKFLOW_13_webhook_name_pattern() -> None:
    with pytest.raises(WorkflowLoadError):
        load_workflow(
            "name: x\non: { webhook: { name: 'a/b', input: p, within: /in } }\n"
            "steps: [{id: a, uses: shell}]\n",
            "wf.yaml",
        )


def test_TRIGGERS_26_dedupe_defaults_to_fingerprint_and_accepts_in_progress() -> None:
    from rifferr.core.workflow import safety_report

    plain, _ = load_workflow(
        "name: x\non:\n  watch: { path: /in }\nsteps: [{id: a, uses: shell}]\n", "wf.yaml"
    )
    assert plain.on is not None
    assert plain.on.watch is not None
    assert plain.on.watch.dedupe == "fingerprint"
    assert not [f for f in safety_report(plain).findings if f.kind == "rerun"]

    wf, _ = load_workflow(
        "name: x\non:\n  watch: { path: /in, dedupe: in_progress }\n"
        "  webhook: { name: hook, input: a.path, dedupe: in_progress }\n"
        "steps: [{id: a, uses: shell}]\n",
        "wf.yaml",
    )
    notes = [f for f in safety_report(wf).findings if f.kind == "rerun"]
    assert [f.step_id for f in notes] == ["on.watch", "on.webhook"]
    assert "imported again" in notes[0].detail
    with pytest.raises(WorkflowLoadError):
        load_workflow(
            "name: x\non:\n  watch: { path: /in, dedupe: off }\nsteps: [{id: a, uses: shell}]\n",
            "wf.yaml",
        )
