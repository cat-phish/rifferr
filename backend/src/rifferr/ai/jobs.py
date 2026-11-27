"""Bulk AI jobs: one task over many targets, with progress and cancel (AI-CORE-19).

Targets run concurrently up to ``ai.concurrency``. A cancel stops new requests; requests
already running finish or time out. One target failing never stops the others.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import JsonValue

from rifferr.ai.base import AITaskResult, AIUsage
from rifferr.ai.runner import AITaskError, add_usage

JobEmit = Callable[[str, str, dict[str, JsonValue]], None]  # profile, type, data
Work = Callable[[], Awaitable[AITaskResult[Any]]]
JobState = Literal["running", "succeeded", "cancelled"]


@dataclass
class AIJob:
    id: str
    profile: str
    task: str
    total: int
    done: int = 0
    failed: int = 0
    skipped: int = 0
    state: JobState = "running"
    usage: AIUsage = field(default_factory=AIUsage)
    errors: dict[str, str] = field(default_factory=dict[str, str])  # target -> message
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task[None] | None = None

    def summary(self) -> dict[str, JsonValue]:
        return {
            "job": self.id,
            "task": self.task,
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cost": str(self.usage.cost) if self.usage.cost is not None else None,
                "currency": self.usage.currency,
                "cost_estimated": self.usage.cost_estimated,
            },
        }


def _no_emit(profile: str, type_: str, data: dict[str, JsonValue]) -> None:
    return None


@dataclass
class AIJobs:
    """Running and recent AI jobs of a server process."""

    emit: JobEmit = _no_emit
    keep: int = 50  # finished jobs kept for status lookups
    jobs: dict[str, AIJob] = field(default_factory=dict[str, AIJob])

    def start(
        self,
        profile: str,
        task: str,
        work: Sequence[tuple[str, Work]],
        *,
        concurrency: int,
        on_result: Callable[[str, AITaskResult[Any] | None, str | None], None] | None = None,
    ) -> AIJob:
        """Start a job over ``work`` (target, coroutine factory) and return it at once."""
        job = AIJob(id=secrets.token_hex(8), profile=profile, task=task, total=len(work))
        self.jobs[job.id] = job
        self._trim()
        job.runner = asyncio.create_task(self._run(job, work, concurrency, on_result))
        return job

    def get(self, job_id: str) -> AIJob | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> AIJob | None:
        job = self.jobs.get(job_id)
        if job is not None and job.state == "running":
            job.cancel_requested.set()
        return job

    async def _run(
        self,
        job: AIJob,
        work: Sequence[tuple[str, Work]],
        concurrency: int,
        on_result: Callable[[str, AITaskResult[Any] | None, str | None], None] | None,
    ) -> None:
        gate = asyncio.Semaphore(max(1, concurrency))

        async def one(target: str, make: Work) -> None:
            async with gate:
                if job.cancel_requested.is_set():
                    job.skipped += 1
                    return
                result: AITaskResult[Any] | None = None
                error: str | None = None
                try:
                    result = await make()
                except AITaskError as exc:
                    error = exc.message
                except Exception as exc:  # a consumer's bug must not kill the job
                    error = f"{type(exc).__name__}: {exc}"
                if result is not None:
                    job.done += 1
                    job.usage = add_usage(job.usage, result.usage)
                else:
                    job.failed += 1
                    job.errors[target] = error or "failed"
                if on_result is not None:
                    on_result(target, result, error)
                self.emit(job.profile, "ai.job.progress", job.summary())

        await asyncio.gather(*(one(t, m) for t, m in work))
        job.state = "cancelled" if job.cancel_requested.is_set() else "succeeded"
        self.emit(job.profile, "ai.job.finished", job.summary())

    def _trim(self) -> None:
        finished = [j for j in self.jobs.values() if j.state != "running"]
        for old in finished[: max(0, len(finished) - self.keep)]:
            del self.jobs[old.id]
