"""Cleanup decisions on the engine side (S-FILES-CLEANUP FILES-CLEANUP-14)."""

from __future__ import annotations

from typing import Protocol

from rifferr.core.files import CleanupDecision


class CleanupDecisionStore(Protocol):
    """Persisted cleanup decisions. The app DB in the worker."""

    def add(self, decision: CleanupDecision) -> None: ...

    def get(self, decision_id: str) -> CleanupDecision | None: ...


class InMemoryCleanupStore:
    def __init__(self) -> None:
        self.items: dict[str, CleanupDecision] = {}

    def add(self, decision: CleanupDecision) -> None:
        if decision.id in self.items:
            raise ValueError(f"cleanup decision {decision.id} exists")
        self.items[decision.id] = decision

    def get(self, decision_id: str) -> CleanupDecision | None:
        return self.items.get(decision_id)
