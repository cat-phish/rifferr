from __future__ import annotations

import textwrap

from rifferr.backends.base import BackendCapabilities
from rifferr.core.workflow import StaticStepRegistry, load_workflow, validate


def check(
    src: str,
    registry: StaticStepRegistry,
    caps: BackendCapabilities | None = None,
    profile_vars: dict[str, str] | None = None,
) -> list[str]:
    wf, doc = load_workflow(textwrap.dedent(src), "wf.yaml")
    return [str(e) for e in validate(wf, registry, caps, profile_vars, doc=doc)]


OK = """\
name: ok
vars:
  inbox: { description: inbox }
steps:
  - id: import
    uses: beets.import
    with: { path: "{{ vars.inbox }}" }
  - id: list
    uses: shell
    with: { run: [ls, "$path", "{{ run.id }}", "{{ steps.import.count }}"] }
"""


def test_WORKFLOW_2_valid_workflow_has_no_errors(registry: StaticStepRegistry) -> None:
    assert check(OK, registry, BackendCapabilities(import_=True), {"inbox": "/in"}) == []


def test_WORKFLOW_2_duplicate_step_ids(registry: StaticStepRegistry) -> None:
    errs = check(
        "name: x\nsteps:\n  - {id: a, uses: shell, with: {run: [ls]}}\n"
        "  - {id: a, uses: shell, with: {run: [ls]}}\n",
        registry,
    )
    assert errs == ["wf.yaml:4:10: steps[1].id: duplicate step id 'a' (first used by steps[0])"]


def test_WORKFLOW_2_unknown_step_type(registry: StaticStepRegistry) -> None:
    errs = check("name: x\nsteps:\n  - id: a\n    uses: shel\n", registry)
    assert errs == [
        "wf.yaml:4:11: steps[0].uses: unknown step type 'shel' (known: beets.import, shell)"
    ]


def test_WORKFLOW_3_apply_is_allowed_on_any_step(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - {id: a, uses: shell, apply: auto, with: {run: [ls]}}\n"
    assert check(src, registry) == []


def test_WORKFLOW_5_with_params_validated_against_step_model(registry: StaticStepRegistry) -> None:
    src = (
        "name: x\nsteps:\n  - id: a\n    uses: shell\n"
        "    with:\n      run: [ls]\n      timeout: soon\n"
    )
    errs = check(src, registry)
    assert len(errs) == 1
    assert errs[0].startswith("wf.yaml:7:16: steps[0].with.timeout:")


def test_WORKFLOW_5_missing_and_unknown_params(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - id: a\n    uses: shell\n    with:\n      cmd: [ls]\n"
    errs = check(src, registry)
    assert "wf.yaml:6:7: steps[0].with.cmd: unknown key" in errs
    assert any(
        e.startswith("wf.yaml:5:5: steps[0].with.run:") and "required" in e.lower() for e in errs
    )


def test_WORKFLOW_5_missing_with_block_reports_on_step(registry: StaticStepRegistry) -> None:
    errs = check("name: x\nsteps:\n  - id: a\n    uses: shell\n", registry)
    assert len(errs) == 1
    assert errs[0].startswith("wf.yaml:3:5: steps[0].with.run:")


def test_WORKFLOW_6_missing_backend_capability(registry: StaticStepRegistry) -> None:
    errs = check(OK, registry, BackendCapabilities(), {"inbox": "/in"})
    assert errs == [
        "wf.yaml:6:11: steps[0].uses: step type 'beets.import' needs backend "
        "capability 'import', which this profile's backend doesn't offer"
    ]


def test_WORKFLOW_6_capabilities_skipped_without_backend(registry: StaticStepRegistry) -> None:
    assert check(OK, registry, None, {"inbox": "/in"}) == []


def test_WORKFLOW_7_undeclared_unsupplied_var(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - id: a\n    uses: shell\n    with: {run: [ls, '{{ vars.lib }}']}\n"
    errs = check(src, registry, None, {})
    assert errs == [
        "wf.yaml:5:22: steps[0].with.run[1]: variable 'lib' is not declared "
        "in vars and not set by the profile"
    ]


def test_WORKFLOW_7_profile_supplied_var_needs_no_declaration(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - id: a\n    uses: shell\n    with: {run: [ls, '{{ vars.lib }}']}\n"
    assert check(src, registry, None, {"lib": "/music"}) == []


def test_WORKFLOW_7_declared_var_without_default_must_be_supplied(
    registry: StaticStepRegistry,
) -> None:
    errs = check(OK, registry, None, {})
    assert errs == [
        "wf.yaml:3:3: vars.inbox: variable has no default and the profile doesn't set it"
    ]


def test_WORKFLOW_7_declared_var_with_default_is_fine(registry: StaticStepRegistry) -> None:
    src = OK.replace("{ description: inbox }", "{ default: /in }")
    assert check(src, registry, None, {}) == []


def test_WORKFLOW_7_profile_checks_skipped_without_profile(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - id: a\n    uses: shell\n    with: {run: [ls, '{{ vars.lib }}']}\n"
    assert check(src, registry, None, None) == []
    assert check(OK, registry, None, None) == []


def test_WORKFLOW_8_unknown_root(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - id: a\n    uses: shell\n    with: {run: ['{{ env.HOME }}']}\n"
    errs = check(src, registry)
    assert errs == [
        "wf.yaml:5:18: steps[0].with.run[0]: unknown root 'env' in "
        "'{{ env.HOME }}' (allowed: vars, run, profile, steps)"
    ]


def test_WORKFLOW_8_unknown_run_and_profile_fields(registry: StaticStepRegistry) -> None:
    src = (
        "name: x\nsteps:\n  - id: a\n    uses: shell\n"
        "    with: {run: ['{{ run.secret }}', '{{ profile.token }}']}\n"
    )
    errs = check(src, registry)
    assert any("unknown field 'secret' for 'run'" in e for e in errs)
    assert any("unknown field 'token' for 'profile'" in e for e in errs)


def test_WORKFLOW_8_step_reference_must_be_an_earlier_step(registry: StaticStepRegistry) -> None:
    src = (
        "name: x\nsteps:\n"
        "  - {id: a, uses: shell, with: {run: ['{{ steps.b.out }}']}}\n"
        "  - {id: b, uses: shell, with: {run: ['{{ steps.a.out }}', '{{ steps.b.out }}']}}\n"
    )
    errs = check(src, registry)
    assert len(errs) == 2
    assert errs[0].startswith("wf.yaml:3:39: steps[0].with.run[0]: '{{ steps.b.out }}' ")
    assert "earlier step" in errs[0]
    assert errs[1].startswith("wf.yaml:4:60: steps[1].with.run[1]:")


def test_WORKFLOW_8_step_reference_needs_an_output_key(registry: StaticStepRegistry) -> None:
    src = (
        "name: x\nsteps:\n  - {id: a, uses: shell, with: {run: [ls]}}\n"
        "  - {id: b, uses: shell, with: {run: ['{{ steps.a }}']}}\n"
    )
    assert any("steps.<id>.<key>" in e for e in check(src, registry))


def test_WORKFLOW_8_expressions_in_when_and_triggers_are_checked(
    registry: StaticStepRegistry,
) -> None:
    src = (
        "name: x\non:\n  watch: { path: '{{ vars.nope }}' }\nsteps:\n"
        "  - {id: a, uses: shell, when: 'genre:{{ bad.x }}', with: {run: [ls]}}\n"
    )
    errs = check(src, registry, None, {})
    assert any(e.startswith("wf.yaml:3:18: on.watch.path:") for e in errs)
    assert any("steps[0].when" in e and "unknown root 'bad'" in e for e in errs)


def test_WORKFLOW_10_malformed_expression_is_an_error(registry: StaticStepRegistry) -> None:
    src = "name: x\nsteps:\n  - {id: a, uses: shell, with: {run: ['{{ vars.x | upper }}']}}\n"
    errs = check(src, registry)
    assert len(errs) == 1
    assert "malformed" in errs[0]
    assert "steps[0].with.run[0]" in errs[0]
