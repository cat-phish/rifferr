"""Import decisions on the engine side (F-IMPORT-UI): the decision store.

Pure apart from injected interfaces.
"""

from __future__ import annotations

from typing import Protocol

from rifferr.core.imports import ImportDecision, ImportDecisionState


class ImportDecisionStore(Protocol):
    """Persisted import decisions (IMPORT-UI-4). The app DB in the worker."""

    def add(self, decision: ImportDecision) -> None: ...

    def get(self, decision_id: str) -> ImportDecision | None: ...

    def for_run(
        self, run_id: str, state: ImportDecisionState | None = None
    ) -> list[ImportDecision]: ...


class InMemoryImportStore:
    def __init__(self) -> None:
        self.items: dict[str, ImportDecision] = {}

    def add(self, decision: ImportDecision) -> None:
        if decision.id in self.items:
            raise ValueError(f"import decision {decision.id} exists")
        self.items[decision.id] = decision

    def get(self, decision_id: str) -> ImportDecision | None:
        return self.items.get(decision_id)

    def for_run(
        self, run_id: str, state: ImportDecisionState | None = None
    ) -> list[ImportDecision]:
        return [
            d
            for d in self.items.values()
            if d.run_id == run_id and (state is None or d.state == state)
        ]

    def save(self, decision: ImportDecision) -> None:
        """Replace a stored decision (tests acting as the worker)."""
        self.items[decision.id] = decision
