"""Pure parts of F-TRIGGERS: durations, payload lookup, fingerprints, settling, dedup.

Nothing here reads the clock or the filesystem; callers pass times and file listings in.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, cast

_UNITS: Final = {"s": 1, "m": 60, "h": 3600}

TriggerKind = Literal["manual", "watch", "schedule", "webhook"]
SkipReason = Literal[
    "in_progress", "already_run", "previously_failed", "previous_still_active", "loose_files"
]

#: Run states that mean "this input is being handled" (TRIGGERS-19).
ACTIVE_STATES: Final = frozenset({"queued", "running", "waiting_review"})
FAILED_STATES: Final = frozenset({"failed", "cancelled"})


def parse_duration(text: str) -> float:
    """Seconds in ``<n>s``, ``<n>m`` or ``<n>h`` (the workflow's ``Duration``)."""
    if len(text) < 2 or text[-1] not in _UNITS or not text[:-1].isdigit():
        raise ValueError(f"'{text}' is not a duration like 30s, 2m or 1h")
    return int(text[:-1]) * _UNITS[text[-1]]


_MISSING: Final = object()


def lookup(document: Any, path: str) -> Any:
    """The value at dotted ``path`` in a parsed JSON ``document``, or ``None``.

    Numeric parts index lists (``trackFiles.0.path``). A missing key, an index out of
    range, or a step into a scalar gives ``None`` (TRIGGERS-15, -16).
    """
    value: Any = document
    for part in path.split("."):
        if isinstance(value, Mapping):
            value = cast(Mapping[str, Any], value).get(part, _MISSING)
        elif isinstance(value, list) and part.isdigit():
            items = cast(list[Any], value)
            value = items[int(part)] if int(part) < len(items) else _MISSING
        else:
            return None
        if value is _MISSING:
            return None
    return value


def fingerprint(files: Iterable[tuple[str, int, int]]) -> str | None:
    """SHA-256 over sorted ``(relative path, size, mtime_ns)``; ``None`` for no files
    (TRIGGERS-19: an empty input is never deduplicated)."""
    entries = sorted(files)
    if not entries:
        return None
    digest = hashlib.sha256()
    for path, size, mtime_ns in entries:
        digest.update(f"{path}\0{size}\0{mtime_ns}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


@dataclass(frozen=True)
class DedupRecord:
    """The last run a trigger (or a manual run) started for one input of a workflow."""

    fingerprint: str
    run_id: str
    run_state: str | None
    """The run's current state; ``None`` when the run record no longer exists."""


def dedup_skip(
    record: DedupRecord | None, current: str | None, *, force: bool = False
) -> SkipReason | None:
    """Why a trigger for an input with fingerprint ``current`` must not start a run, or
    ``None`` if it may (TRIGGERS-19, -20)."""
    if force or current is None or record is None or record.fingerprint != current:
        return None
    if record.run_state in ACTIVE_STATES:
        return "in_progress"
    if record.run_state == "succeeded":
        return "already_run"
    if record.run_state in FAILED_STATES:
        return "previously_failed"
    return None


@dataclass
class SettleTracker:
    """Candidate inputs waiting for no change during ``settle`` seconds (TRIGGERS-9).

    ``touch`` records a change under a candidate; ``due`` hands out (and forgets) the
    candidates whose last change is at least ``settle`` seconds old.
    """

    settle: float
    _last: dict[str, float] = field(default_factory=dict[str, float])

    def touch(self, name: str, now: float) -> None:
        self._last[name] = now

    def discard(self, name: str) -> None:
        self._last.pop(name, None)

    def clear(self) -> None:
        self._last.clear()

    def pending(self) -> Sequence[str]:
        return sorted(self._last)

    def due(self, now: float) -> list[str]:
        ready = sorted(n for n, t in self._last.items() if now - t >= self.settle)
        for name in ready:
            del self._last[name]
        return ready

    def next_deadline(self) -> float | None:
        return min(self._last.values()) + self.settle if self._last else None


def top_level(relative: str) -> str | None:
    """The top-level entry a path relative to the watch folder belongs to, or ``None``
    for the watch folder itself."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p and p != "."]
    return parts[0] if parts else None
