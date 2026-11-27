"""Bulk AI jobs (AI-CORE-19)."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import JsonValue

from rifferr.ai.base import AITaskResult, AIUsage
from rifferr.ai.jobs import AIJobs
from rifferr.ai.runner import AITaskError


def result(tokens: int = 10) -> AITaskResult[Any]:
    usage = AIUsage(input_tokens=tokens, output_tokens=1)
    return AITaskResult[Any](value=None, usage=usage, provider="p", model="m", prompt_version=1)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, JsonValue]]] = []

    def __call__(self, profile: str, type_: str, data: dict[str, JsonValue]) -> None:
        self.events.append((profile, type_, data))


async def test_AI_CORE_19_runs_concurrently_up_to_limit() -> None:
    running = 0
    peak = 0

    async def work() -> AITaskResult[Any]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return result()

    rec = Recorder()
    jobs = AIJobs(emit=rec)
    job = jobs.start("main", "t", [(str(i), work) for i in range(6)], concurrency=2)
    assert job.runner is not None
    await job.runner
    assert peak == 2
    assert (job.state, job.done, job.failed) == ("succeeded", 6, 0)
    assert job.usage.input_tokens == 60
    types = [t for _, t, _ in rec.events]
    assert types.count("ai.job.progress") == 6
    assert types[-1] == "ai.job.finished"
    assert rec.events[-1][0] == "main"


async def test_AI_CORE_19_one_failure_does_not_stop_others() -> None:
    async def ok() -> AITaskResult[Any]:
        return result()

    async def bad() -> AITaskResult[Any]:
        raise AITaskError("HTTP 500")

    async def buggy() -> AITaskResult[Any]:
        raise RuntimeError("consumer bug")

    seen: list[tuple[str, str | None]] = []
    jobs = AIJobs()
    job = jobs.start(
        "main",
        "t",
        [("a", ok), ("b", bad), ("c", buggy), ("d", ok)],
        concurrency=1,
        on_result=lambda target, r, err: seen.append((target, err)),
    )
    assert job.runner is not None
    await job.runner
    assert (job.done, job.failed) == (2, 2)
    assert job.errors == {"b": "HTTP 500", "c": "RuntimeError: consumer bug"}
    assert seen == [
        ("a", None),
        ("b", "HTTP 500"),
        ("c", "RuntimeError: consumer bug"),
        ("d", None),
    ]


async def test_AI_CORE_19_cancel_stops_new_requests_running_ones_finish() -> None:
    gate = asyncio.Event()
    started: list[str] = []

    def make(name: str) -> Any:
        async def work() -> AITaskResult[Any]:
            started.append(name)
            await gate.wait()
            return result()

        return work

    jobs = AIJobs()
    job = jobs.start("main", "t", [(n, make(n)) for n in "abcde"], concurrency=2)
    await asyncio.sleep(0.01)
    assert started == ["a", "b"]
    cancelled = jobs.cancel(job.id)
    assert cancelled is job
    gate.set()
    assert job.runner is not None
    await job.runner
    assert started == ["a", "b"]
    assert (job.state, job.done, job.skipped) == ("cancelled", 2, 3)
    assert jobs.cancel("nope") is None


async def test_AI_CORE_19_finished_jobs_are_trimmed() -> None:
    async def ok() -> AITaskResult[Any]:
        return result()

    jobs = AIJobs(keep=2)
    for _ in range(4):
        job = jobs.start("main", "t", [("a", ok)], concurrency=1)
        assert job.runner is not None
        await job.runner
    assert len(jobs.jobs) <= 3
