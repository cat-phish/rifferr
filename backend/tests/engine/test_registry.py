from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from rifferr.engine import StepTypeRegistry
from rifferr.steps.registry import load_step_types
from tests.engine.steps import CrashStep, EchoStep


@dataclass
class FakeEntryPoint:
    name: str
    obj: Any
    value: str = "pkg.mod:Obj"

    def load(self) -> Any:
        if isinstance(self.obj, Exception):
            raise self.obj
        return self.obj


def test_RUN_3_step_types_load_from_entry_points() -> None:
    eps = [FakeEntryPoint("test.echo", EchoStep), FakeEntryPoint("test.crash", CrashStep())]
    registry = load_step_types(entry_points=lambda group: eps if group == "rifferr.steps" else [])
    assert registry.names() == ["test.crash", "test.echo"]
    assert registry.errors == []
    info = registry.get("test.echo")
    assert info is not None
    assert info.params_model is EchoStep.params_model


def test_RUN_3_broken_entry_points_are_reported_not_fatal() -> None:
    class NoRun:
        name = "bad.norun"
        params_model = EchoStep.params_model
        requires = EchoStep.requires

    eps = [
        FakeEntryPoint("test.echo", EchoStep),
        FakeEntryPoint("bad.import", ImportError("no module x")),
        FakeEntryPoint("bad.norun", NoRun),
        FakeEntryPoint("bad.name", EchoStep),  # registered name differs from step.name
    ]
    registry = load_step_types(entry_points=lambda group: eps)
    assert registry.names() == ["test.echo"]
    messages = "\n".join(registry.errors)
    assert "bad.import" in messages
    assert "no module x" in messages
    assert "bad.norun" in messages
    assert "missing run" in messages
    assert "bad.name" in messages
    assert "test.echo" in messages


def test_RUN_3_installed_rifferr_registers_through_entry_points() -> None:
    registry = load_step_types()
    assert registry.errors == []


def test_RUN_4_registry_rejects_objects_missing_the_step_protocol() -> None:
    class Partial:
        name = "x.partial"

    with pytest.raises(TypeError, match="params_model"):
        StepTypeRegistry([Partial()])  # type: ignore[list-item]


def test_RUN_4_duplicate_names_rejected() -> None:
    with pytest.raises(ValueError, match=r"test\.echo"):
        StepTypeRegistry([EchoStep(), EchoStep()])
