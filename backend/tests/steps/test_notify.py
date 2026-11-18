"""S-NOTIFY in the engine: the ``notify`` step (NOTIFY-11 to -13) and ``ctx.notify``."""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.core.notify import Notification, SendResult
from rifferr.core.profile import Redactor
from rifferr.core.workflow import load_workflow
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.executor import RunValidationError
from rifferr.steps.notify import NotifyParams, NotifyStep
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = StepTypeRegistry([*ALL, NotifyStep()])


class FakeTargets:
    def __init__(self, names: list[str], fail: set[str] | None = None, base_url: str | None = None):
        self._names = names
        self.fail = fail or set()
        self.base_url = base_url
        self.sent: list[tuple[str, Notification]] = []

    def names(self) -> list[str]:
        return list(self._names)

    async def send(self, target: str, n: Notification) -> SendResult:
        self.sent.append((target, n))
        if target in self.fail:
            return SendResult(target=target, ok=False, error="ntfy is down")
        return SendResult(target=target, ok=True)


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.engine.registry = REGISTRY
    return harness


def start(h: Harness, steps: str, targets: FakeTargets | None) -> Run:
    text = "name: tell\nsteps:\n" + textwrap.indent(textwrap.dedent(steps), "  ")
    wf, doc = load_workflow(text, "wf.yaml")
    return h.engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=h.backend,
        doc=doc,
        redactor=Redactor([]),
        notify=targets,
    )


def album(h: Harness, run: Run, name: str, **fields: Any) -> None:
    h.backend.add_album(
        {
            "albumartist": "Artist",
            "album": name,
            RUN_MARKER: run.id,
            **fields,
            "items": [{"title": f"{name} 1", "track": 1}],
        }
    )


def events(h: Harness, run: Run, type: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.events(run) if e.type == type]


def outputs(h: Harness, run: Run) -> dict[str, Any]:
    (finished,) = [e for e in events(h, run, "run.step.finished")]
    return dict(finished["outputs"])


def test_NOTIFY_11_parameters_and_defaults() -> None:
    p = NotifyParams.model_validate({"title": "t", "body": "b"})
    assert (p.targets, p.type, p.per, p.link, p.required, p.skip_empty) == (
        None,
        "info",
        "run",
        "run",
        False,
        False,
    )
    with pytest.raises(ValueError, match="body"):
        NotifyParams.model_validate({"title": "t"})


def test_NOTIFY_11_per_run_is_run_level() -> None:
    assert NotifyStep.run_level({})
    assert NotifyStep.run_level({"per": "run"})
    assert not NotifyStep.run_level({"per": "album"})


async def test_NOTIFY_11_per_run_interpolates_and_sends_once_without_albums(h: Harness) -> None:
    targets = FakeTargets(["phone", "discord"], base_url="https://r.lan")
    run = start(
        h,
        "- {id: tell, uses: notify, with: {title: 'Run {{ run.id }}',"
        " body: 'from {{ profile.name }}'}}\n",
        targets,
    )
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert [t for t, _ in targets.sent] == ["phone", "discord"]
    n = targets.sent[0][1]
    assert n.title == f"Run {run.id}"
    assert n.body == "from main"
    assert n.url == f"https://r.lan/runs/{run.id}"
    assert outputs(h, run) == {"sent": "discord,phone", "failed": "", "messages": 1}


async def test_NOTIFY_11_skip_empty(h: Harness) -> None:
    targets = FakeTargets(["phone"])
    run = start(
        h,
        "- {id: tell, uses: notify, when: 'comp:1', with: {title: t, body: b, skip_empty: true}}\n",
        targets,
    )
    album(h, run, "A", comp=0)
    await h.engine.execute(run)
    assert targets.sent == []
    assert outputs(h, run)["messages"] == 0


async def test_NOTIFY_11_per_album_evaluates_beets_templates(h: Harness) -> None:
    targets = FakeTargets(["phone"])
    run = start(
        h,
        "- {id: tell, uses: notify, with: {per: album, title: 'Imported: $album',"
        " body: '{{ profile.name }}: $albumartist', link: review}}\n",
        targets,
    )
    album(h, run, "A")
    album(h, run, "B")
    await h.engine.execute(run)
    titles = sorted(n.title for _, n in targets.sent)
    assert titles == ["Imported: A", "Imported: B"]
    assert {n.body for _, n in targets.sent} == {"main: Artist"}
    assert all(n.url is None for _, n in targets.sent)  # no base_url
    assert outputs(h, run)["messages"] == 2


async def test_NOTIFY_11_template_error_fails_that_album(h: Harness) -> None:
    targets = FakeTargets(["phone"])
    run = start(
        h,
        "- {id: tell, uses: notify, on_error: continue,"
        " with: {per: album, title: '%nosuch{$album}', body: b}}\n",
        targets,
    )
    album(h, run, "A")
    await h.engine.execute(run)
    (finished,) = events(h, run, "run.step.finished")
    assert finished["failed_targets"]
    assert targets.sent == []


async def test_NOTIFY_12_failed_send_warns_and_step_succeeds(h: Harness) -> None:
    targets = FakeTargets(["phone", "discord"], fail={"phone"})
    run = start(h, "- {id: tell, uses: notify, with: {title: t, body: b}}\n", targets)
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert outputs(h, run) == {"sent": "discord", "failed": "phone", "messages": 1}
    assert any(
        "notification to phone failed: ntfy is down" in line for line in h.logs(run, "error")
    )


async def test_NOTIFY_12_required_fails_the_step(h: Harness) -> None:
    targets = FakeTargets(["phone"], fail={"phone"})
    run = start(
        h, "- {id: tell, uses: notify, with: {title: t, body: b, required: true}}\n", targets
    )
    await h.engine.execute(run)
    assert run.state is RunState.failed


async def test_NOTIFY_4_step_sends_emit_notify_events(h: Harness) -> None:
    targets = FakeTargets(["phone", "discord"], fail={"phone"})
    run = start(h, "- {id: tell, uses: notify, with: {title: t, body: b}}\n", targets)
    await h.engine.execute(run)
    assert events(h, run, "notify.sent") == [{"target": "discord", "source": "step"}]
    assert events(h, run, "notify.failed") == [
        {"target": "phone", "source": "step", "error": "ntfy is down"}
    ]


async def test_NOTIFY_13_unknown_target_is_a_validation_error(h: Harness) -> None:
    with pytest.raises(RunValidationError) as exc:
        start(
            h,
            "- {id: tell, uses: notify, with: {title: t, body: b, targets: [pager]}}\n",
            FakeTargets(["phone"]),
        )
    assert "unknown notification target 'pager' (this profile has: phone)" in str(exc.value)


async def test_NOTIFY_13_profile_without_targets(h: Harness) -> None:
    with pytest.raises(RunValidationError):
        start(h, "- {id: tell, uses: notify, with: {title: t, body: b, targets: [phone]}}\n", None)
    run = start(h, "- {id: tell, uses: notify, with: {title: t, body: b}}\n", None)
    await h.engine.execute(run)
    assert run.state is RunState.failed  # no targets at all: say so


async def test_NOTIFY_13_needs_no_write_capability() -> None:
    from rifferr.backends.base import BackendCapabilities

    assert NotifyStep.requires == BackendCapabilities(query=True, templates=True)
