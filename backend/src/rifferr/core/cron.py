"""Cron schedules for ``on.schedule`` (F-TRIGGERS TRIGGERS-12, -13). Pure: no clock."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from cronsim import CronSim, CronSimError

ALIASES: Final = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    """A schedule that isn't a valid five-field cron expression or alias."""


def parse_cron(expr: str) -> str:
    """The five-field form of ``expr``; raises :class:`CronError` if it isn't valid."""
    text = ALIASES.get(expr.strip().lower(), expr.strip())
    if len(text.split()) != 5:
        raise CronError(
            f"'{expr}' is not a cron expression: use five fields "
            "(minute hour day-of-month month day-of-week) or an alias like @daily"
        )
    try:
        CronSim(text, datetime(2000, 1, 1))
    except CronSimError as exc:
        raise CronError(f"'{expr}' is not a valid cron expression: {exc}") from None
    return text


def next_fire(expr: str, after: datetime) -> datetime:
    """The first firing strictly after ``after``, in ``after``'s time zone.

    ``after`` must be aware. Times that fall in a DST gap fire right after the gap, and
    times that DST repeats fire once (cronsim's behavior, as cron does).
    """
    if after.tzinfo is None:
        raise ValueError("next_fire needs an aware datetime")
    return next(CronSim(parse_cron(expr), after))
