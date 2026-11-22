"""Which steps can put something in the review queue (UI-REFRESH-13, ``waits``)."""

from __future__ import annotations

from typing import Any

from rifferr.core.workflow.models import StepSpec
from rifferr.engine.executor import may_wait


class _Type:
    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


def spec(**fields: Any) -> StepSpec:
    return StepSpec.model_validate({"id": "s", "uses": "x.y", **fields})


def test_UI_REFRESH_13_may_wait_follows_waits_and_apply() -> None:
    assert may_wait(spec(), _Type()) is False  # no ``waits``: never
    assert may_wait(spec(apply="auto"), _Type(waits="always")) is True
    assert may_wait(spec(), _Type(waits="apply")) is True  # apply defaults to review
    assert may_wait(spec(apply="auto"), _Type(waits="apply")) is False
    assert may_wait(spec(), _Type(waits="apply", default_apply="auto")) is False
    assert may_wait(spec(apply="auto"), _Type(waits="apply", force_review=True)) is True
