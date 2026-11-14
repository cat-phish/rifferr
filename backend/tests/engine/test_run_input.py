"""F-TRIGGERS TRIGGERS-5, -6: ``{{ run.input }}`` and ``{{ run.trigger }}``."""

from __future__ import annotations

from tests.engine.conftest import Harness

ECHO = """\
name: x
{extra}steps:
  - {{id: a, uses: test.echo, with: {{message: "[{{{{ run.input }}}}] {{{{ run.trigger }}}}"}}}}
"""


async def test_TRIGGERS_5_run_input_and_trigger_are_in_the_context(h: Harness) -> None:
    run = h.run(ECHO.format(extra=""), run_input="/in/Artist - Album", trigger="watch")
    await h.engine.execute(run)
    assert h.logs(run) == ["[/in/Artist - Album] watch"]
    assert (run.input, run.trigger) == ("/in/Artist - Album", "watch")


async def test_TRIGGERS_5_manual_run_without_input_is_empty(h: Harness) -> None:
    run = h.run(ECHO.format(extra=""))
    await h.engine.execute(run)
    assert h.logs(run) == ["[] manual"]


async def test_TRIGGERS_6_workflow_input_is_the_default(h: Harness) -> None:
    src = ECHO.format(extra="input: '{{ vars.inbox }}/{{ profile.name }}'\n")
    run = h.run(src, profile_vars={"inbox": "/in"}, trigger="schedule")
    await h.engine.execute(run)
    assert h.logs(run) == ["[/in/main] schedule"]


async def test_TRIGGERS_6_trigger_input_overrides_workflow_input(h: Harness) -> None:
    src = ECHO.format(extra="input: /in\n")
    run = h.run(src, run_input="/in/A", trigger="webhook")
    await h.engine.execute(run)
    assert h.logs(run) == ["[/in/A] webhook"]
