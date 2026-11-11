from __future__ import annotations

import pytest

from rifferr.core.workflow import WorkflowLoadError, load_workflow


def test_WORKFLOW_13_on_block_parses_all_triggers() -> None:
    wf, _ = load_workflow(
        "name: x\non:\n  watch: { path: '{{ vars.inbox }}', settle: 10m }\n"
        "  schedule: '0 4 * * *'\n  webhook: { name: lidarr }\n"
        "steps: [{id: a, uses: shell}]\n",
        "wf.yaml",
    )
    assert wf.on is not None
    assert wf.on.watch is not None
    assert wf.on.watch.path == "{{ vars.inbox }}"
    assert wf.on.watch.settle == "10m"
    assert wf.on.schedule == "0 4 * * *"
    assert wf.on.webhook is not None
    assert wf.on.webhook.name == "lidarr"


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
            "name: x\non: { webhook: { name: 'a/b' } }\nsteps: [{id: a, uses: shell}]\n", "wf.yaml"
        )
