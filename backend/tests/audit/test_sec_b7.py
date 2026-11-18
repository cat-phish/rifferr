# ruff: noqa: F811, PT018
"""Audit B7 (secrets / logs / child environments) findings SEC-101..105.
Each test asserts the SAFE behavior."""

from __future__ import annotations

import json
import logging
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from rifferr.backends.base import BackendCapabilities
from rifferr.backends.fake import FakeBackend
from rifferr.core.covers import CoverCandidate, CoverQuery, CoverSettings, ProviderSetting
from rifferr.core.profile import ProfileLoadError, SecretRef, parse_profile
from rifferr.core.workflow import load_workflow
from rifferr.covers.http import CoverHttp
from rifferr.covers.search import search
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.engine import (
    Engine,
    InMemoryEventSink,
    StepContext,
    StepResult,
    StepTypeRegistry,
)
from rifferr.worker.previews import PreviewSupervisor
from rifferr.worker.supervisor import WorkerSupervisor
from tests.steps.conftest import ShellHarness, sh  # noqa: F401  (fixture)
from tests.steps.test_shell import PRINT_ENV, PY, step

ML = "line-one-AAAAAAAA\nline-two-BBBBBBBB\nline-three-CCCCCCCC"


def _all_text(sh: ShellHarness, run: Any) -> str:
    return json.dumps([e.data for e in sh.sink.since(run.id, 0)])


def _ml_secrets(sh: ShellHarness, value: str) -> ProfileSecrets:
    return ProfileSecrets(
        {"key": SecretRef.parse("keyring:svc/key")},
        SecretResolver(env=sh.environ, keyring_get=lambda s, n: value),
    )


async def test_SEC_101_multiline_secret_is_masked_in_step_log(sh: ShellHarness) -> None:
    code = "import os; print(os.environ['RIFFERR_SECRET_KEY'])"
    src = step({"run": [PY, "-c", code], "per": "run", "secrets": ["key"]})
    run = await sh.run(src, secrets=_ml_secrets(sh, ML))
    raw = _all_text(sh, run)
    for line in ML.splitlines():
        assert line not in raw, f"secret line leaked: {line}"


async def test_SEC_101_secret_across_line_cut(sh: ShellHarness) -> None:
    secret = "tok-ABCDEFGHIJKL"
    code = (
        "import os, sys; sys.stdout.write('x' * (64 * 1024 - 3) + os.environ['RIFFERR_SECRET_KEY'])"
    )
    src = step({"run": [PY, "-c", code], "per": "run", "secrets": ["key"]})
    run = await sh.run(src, secrets=_ml_secrets(sh, secret))
    raw = _all_text(sh, run)
    assert secret not in raw
    assert "ABCDEFGHIJKL" not in raw and "tok-ABC" not in raw.replace("x", "")


class _P(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeakyStep:
    name = "test.leaky"
    run_level = True
    params_model = _P
    requires = BackendCapabilities()

    async def run(self, ctx: StepContext) -> StepResult:
        value = ctx.secret("k").reveal()
        ctx.fail_target("album-1", f"bad {value}")
        return StepResult(outputs={"echo": value})


async def test_SEC_102_step_finished_event_is_redacted() -> None:
    value = "supersecret-value-123"
    sink = InMemoryEventSink()
    engine = Engine(StepTypeRegistry([LeakyStep()]), sink)
    text = "name: t\nsteps:\n  - {id: a, uses: test.leaky}\n"
    wf, doc = load_workflow(text, "wf.yaml")
    run = engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=FakeBackend.from_fixture({"albums": []}),
        doc=doc,
        secrets=ProfileSecrets({"k": SecretRef.parse("env:K")}, SecretResolver(env={"K": value})),
    )
    await engine.execute(run)
    finished = [e for e in sink.since(run.id, 0) if e.type == "run.step.finished"]
    assert finished, [e.type for e in sink.since(run.id, 0)]
    assert value not in json.dumps([e.data for e in finished])


async def test_SEC_103_cover_provider_error_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    token = "tok-SECRET-VALUE-9"

    class Keyed:
        name, hosts, needs = "keyed", frozenset[str](), True

        async def search(self, query: CoverQuery, settings: Any, http: CoverHttp) -> Any:
            raise RuntimeError(f"GET https://api.test/?token={settings['token']} failed")
            yield CoverCandidate.make("keyed", path="/k.jpg", match_confidence=1)

    caplog.set_level(logging.DEBUG)
    s = CoverSettings(providers=[ProviderSetting(name="keyed", settings={"secret": "k"})])
    seen: list[str] = []
    async with httpx.AsyncClient() as client:
        out = await search(
            CoverQuery(artist="A", album="B"),
            s,
            {"keyed": Keyed()},  # type: ignore[dict-item]
            client,
            secret=lambda n: token,
            on_provider=lambda st: seen.append(st.model_dump_json()),
        )
    blob = json.dumps([x.message for x in out.statuses]) + "".join(seen)
    assert token not in blob
    logged = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    for r in caplog.records:
        if r.exc_info:
            import traceback

            logged += "".join(traceback.format_exception(*r.exc_info))
    assert token not in logged


async def test_SEC_104_shell_env_hides_other_profiles_env_secrets(sh: ShellHarness) -> None:
    sh.environ["A_TOKEN"] = "tok-a-123456"
    sh.environ["B_TOKEN"] = "tok-b-123456"
    secrets = ProfileSecrets(
        {"a": SecretRef.parse("env:A_TOKEN")}, SecretResolver(env=sh.environ)
    )  # profile "a" only; profile "b" (env:B_TOKEN) is another profile on the same server
    run = await sh.run(step({"run": [PY, "-c", PRINT_ENV], "per": "run"}), secrets=secrets)
    env = json.loads(sh.lines(run)[0])
    assert "A_TOKEN" not in env
    assert "B_TOKEN" not in env


def test_SEC_104_var_from_secret_env_is_rejected() -> None:
    text = textwrap.dedent(
        """\
        vars:
          t: "{{ env.A_TOKEN }}"
        secrets:
          a: env:A_TOKEN
        """
    )
    with pytest.raises(ProfileLoadError):
        parse_profile(
            text, "profile.yaml", name="main", root=Path("/cfg"), env={"A_TOKEN": "tok-a-123456"}
        )


class _Stderr:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def __aiter__(self) -> AsyncIterator[bytes]:
        async def gen() -> AsyncIterator[bytes]:
            for line in self.lines:
                yield line

        return gen()


@pytest.mark.parametrize("cls", [WorkerSupervisor, PreviewSupervisor])
async def test_SEC_105_worker_stderr_control_chars_are_escaped(
    cls: Any, caplog: pytest.LogCaptureFixture
) -> None:
    handle = SimpleNamespace(
        proc=SimpleNamespace(stderr=_Stderr([b"bad \x1b[31mred\rfake line\x08\n"]))
    )
    caplog.set_level(logging.DEBUG)
    await cls._log_stderr(object(), handle)
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("rifferr.worker")]
    assert msgs
    for m in msgs:
        assert all(c == "\t" or ord(c) >= 0x20 for c in m), repr(m)
    _ = sys
