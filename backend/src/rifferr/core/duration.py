"""Durations written in workflow files: ``30m``, ``12h``, ``3d``, ``1d12h`` (S-REVIEW)."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated, Final

from pydantic import AfterValidator, Field

DURATION_PATTERN: Final = r"^(\d+[dhms])+$"
_PART: Final = re.compile(r"(\d+)([dhms])")
_UNIT: Final = {"d": "days", "h": "hours", "m": "minutes", "s": "seconds"}


def parse_duration(text: str) -> timedelta:
    """``"1d12h"`` -> 36 hours. Raises ``ValueError`` for anything else, or zero."""
    if not re.fullmatch(DURATION_PATTERN, text):
        raise ValueError(f"{text!r} is not a duration like 30m, 12h or 3d")
    total = timedelta()
    for number, unit in _PART.findall(text):
        total += timedelta(**{_UNIT[unit]: int(number)})
    if total <= timedelta():
        raise ValueError(f"duration {text!r} must be longer than zero")
    return total


def _check(text: str) -> str:
    parse_duration(text)
    return text


Duration = Annotated[
    str,
    Field(
        pattern=DURATION_PATTERN,
        description="A duration: whole numbers with d, h, m or s, e.g. 30m, 12h, 3d, 1d12h",
    ),
    AfterValidator(_check),
]
"""A duration as written in a workflow file; ``parse_duration`` turns it into a timedelta."""
