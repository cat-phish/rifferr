"""A fixed, tiny request for the Settings page's Test connection (F-SETTINGS SETTINGS-7).
No album data; not an entry point, so workflows can't run it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from rifferr.ai.base import TaskContext, TaskOutcome

PING_TASK = "connection.test"


class PingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PingAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ok: bool


class PingTask:
    id = PING_TASK
    prompt_version = 1
    input_model = PingInput
    output_model = PingAnswer

    def sends(self, inp: PingInput) -> list[str]:
        return ["a fixed test message"]

    def files(self, inp: PingInput) -> Mapping[str, str]:
        return {}

    def build(self, inp: PingInput, ctx: TaskContext) -> tuple[str, str]:
        return ("This is a connection test.", 'Answer with the JSON {"ok": true}.')

    def validate(self, answer: Any, inp: PingInput, ctx: TaskContext) -> TaskOutcome[bool]:
        assert isinstance(answer, PingAnswer)
        return TaskOutcome[bool](value=answer.ok)
