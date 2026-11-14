"""F-TRIGGERS: the ``on:`` block's fields, arm-time interpolation, cron (pure parts)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from rifferr.core.cron import CronError, next_fire, parse_cron
from rifferr.core.workflow import StaticStepRegistry, WorkflowLoadError, load_workflow, validate
from tests.core.conftest import SHELL

AMS = ZoneInfo("Europe/Amsterdam")


def load(on: str, extra: str = "") -> str:
    return f"name: x\n{extra}on: {on}\nsteps: [{{id: a, uses: shell, with: {{run: [echo]}}}}]\n"


def errors(src: str, profile_vars: dict[str, str] | None = None) -> list[str]:
    wf, doc = load_workflow(src, "wf.yaml")
    return [
        e.message for e in validate(wf, StaticStepRegistry([SHELL]), None, profile_vars, doc=doc)
    ]


# -- TRIGGERS-3: arm-time values only see vars and profile ---------------------------


@pytest.mark.parametrize(
    ("on", "extra"),
    [
        ("{watch: {path: '{{ run.id }}'}}", ""),
        ("{watch: {path: '{{ steps.a.count }}'}}", ""),
        ("{webhook: {name: h, input: p, within: '{{ run.input }}'}}", ""),
        ("{schedule: '@daily'}", "input: '{{ run.workflow }}/x'\n"),
    ],
)
def test_TRIGGERS_3_arm_time_values_reject_run_and_steps(on: str, extra: str) -> None:
    problems = errors(load(on, extra))
    assert any("resolved before any run exists" in p for p in problems), problems


def test_TRIGGERS_3_arm_time_values_may_use_vars_and_profile() -> None:
    src = load(
        "{watch: {path: '{{ vars.inbox }}'}, webhook: {name: h, input: p}}",
        "input: '{{ vars.inbox }}/{{ profile.name }}'\n",
    )
    assert errors(src, {"inbox": "/in"}) == []


def test_TRIGGERS_3_other_on_fields_are_not_templates() -> None:
    with pytest.raises(WorkflowLoadError):
        load_workflow(load("{watch: {path: /in, settle: '{{ vars.s }}'}}"), "wf.yaml")


# -- TRIGGERS-9, -10: settle and poll -------------------------------------------------


def test_TRIGGERS_9_settle_defaults_to_two_minutes() -> None:
    wf, _ = load_workflow(load("{watch: {path: /in}}"), "wf.yaml")
    assert wf.on is not None
    assert wf.on.watch is not None
    assert wf.on.watch.settle == "2m"
    assert wf.on.watch.poll is None


@pytest.mark.parametrize("value", ["10", "0s", "2d", "1.5m", "-1m"])
def test_TRIGGERS_9_settle_must_be_a_duration(value: str) -> None:
    with pytest.raises(WorkflowLoadError):
        load_workflow(load(f"{{watch: {{path: /in, settle: '{value}'}}}}"), "wf.yaml")


@pytest.mark.parametrize(("value", "ok"), [("4s", False), ("5s", True), ("1m", True)])
def test_TRIGGERS_10_poll_interval_floor(value: str, ok: bool) -> None:
    src = load(f"{{watch: {{path: /in, poll: '{value}'}}}}")
    if ok:
        load_workflow(src, "wf.yaml")
    else:
        with pytest.raises(WorkflowLoadError, match="at least 5s"):
            load_workflow(src, "wf.yaml")


# -- TRIGGERS-15, -16: webhook fields --------------------------------------------------


def test_TRIGGERS_16_webhook_needs_within_or_watch() -> None:
    with pytest.raises(WorkflowLoadError, match="within"):
        load_workflow(load("{webhook: {name: h, input: p}}"), "wf.yaml")
    load_workflow(load("{webhook: {name: h, input: p, within: /in}}"), "wf.yaml")
    load_workflow(load("{watch: {path: /in}, webhook: {name: h, input: p}}"), "wf.yaml")


@pytest.mark.parametrize("path", ["trackFiles.0.path", "path", "a_b.c-d"])
def test_TRIGGERS_15_webhook_input_is_a_dotted_path(path: str) -> None:
    load_workflow(load(f"{{webhook: {{name: h, input: '{path}', within: /in}}}}"), "wf.yaml")


@pytest.mark.parametrize("path", ["", "a..b", ".a", "a b", "$.a", "{{ vars.x }}"])
def test_TRIGGERS_15_webhook_input_rejects_other_text(path: str) -> None:
    with pytest.raises(WorkflowLoadError):
        load_workflow(load(f"{{webhook: {{name: h, input: '{path}', within: /in}}}}"), "wf.yaml")


# -- TRIGGERS-12, -13: cron -------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "five"),
    [("0 4 * * *", "0 4 * * *"), ("@daily", "0 0 * * *"), ("@Hourly", "0 * * * *")],
)
def test_TRIGGERS_12_cron_forms(expr: str, five: str) -> None:
    assert parse_cron(expr) == five


@pytest.mark.parametrize("expr", ["", "0 4 * *", "0 4 * * * *", "61 * * * *", "@reboot", "x"])
def test_TRIGGERS_12_invalid_cron_is_rejected(expr: str) -> None:
    with pytest.raises(CronError):
        parse_cron(expr)
    with pytest.raises(WorkflowLoadError):
        load_workflow(load(f"{{schedule: '{expr}'}}"), "wf.yaml")


def test_TRIGGERS_12_next_fire_in_profile_time_zone() -> None:
    after = datetime(2026, 1, 10, 12, 0, tzinfo=AMS)
    assert next_fire("0 4 * * *", after) == datetime(2026, 1, 11, 4, 0, tzinfo=AMS)


def test_TRIGGERS_12_next_fire_is_strictly_after() -> None:
    at = datetime(2026, 1, 11, 4, 0, tzinfo=AMS)
    assert next_fire("0 4 * * *", at) == datetime(2026, 1, 12, 4, 0, tzinfo=AMS)


def test_TRIGGERS_12_dst_gap_fires_right_after_the_gap() -> None:
    # 2026-03-29 02:00-03:00 doesn't exist in Amsterdam.
    fired = next_fire("30 2 * * *", datetime(2026, 3, 28, 12, 0, tzinfo=AMS))
    assert fired == datetime(2026, 3, 29, 3, 0, tzinfo=AMS)


def test_TRIGGERS_12_dst_repeat_fires_once() -> None:
    # 2026-10-25 02:00-03:00 happens twice in Amsterdam.
    first = next_fire("30 2 * * *", datetime(2026, 10, 24, 12, 0, tzinfo=AMS))
    second = next_fire("30 2 * * *", first)
    assert (first.month, first.day) == (10, 25)
    assert (second.month, second.day) == (10, 26)


def test_TRIGGERS_12_next_fire_needs_an_aware_time() -> None:
    with pytest.raises(ValueError, match="aware"):
        next_fire("0 4 * * *", datetime(2026, 1, 1))
