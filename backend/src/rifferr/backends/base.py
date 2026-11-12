"""LibraryBackend protocol and capabilities (C-BACKEND).

The engine, steps, UI, covers and AI see a music library only through this
protocol and the snapshot models in ``rifferr.core``. Implementations register
under the ``rifferr.backends`` entry-point group.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from rifferr.core.changeset import ApplyResult, ChangeProblem, ChangeSet
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot

#: Flexible attribute holding the id of the run that imported or last touched an album.
RUN_MARKER: Final = "rifferr_run"

ImportMode = Literal["quiet", "auto", "interactive"]

#: Called with an import decision request; shape defined by F-IMPORT-UI (M2).
#: Unused in M0, where only ``quiet`` imports exist.
DecisionCallback = Callable[[object], object]


class CapabilityError(RuntimeError):
    """A method was called that this backend doesn't support (BACKEND-2)."""


class QueryError(ValueError):
    """A condition or query string is invalid for this backend."""


class TemplateError(ValueError):
    """A template is invalid or uses something this backend doesn't support."""


class ChangeSetRejected(ValueError):
    """``apply`` was given changes that ``check`` refuses (CHANGESET-2). Nothing was applied."""

    def __init__(self, problems: list[ChangeProblem]) -> None:
        self.problems = problems
        super().__init__("; ".join(f"{p.target.label()} {p.field}: {p.message}" for p in problems))


class BackendCapabilities(BaseModel):
    """What a library backend can do (BACKEND-2).

    Step types declare the capabilities they need with the same model: a field
    set to ``True`` means "required".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    import_: bool = False
    query: bool = False
    templates: bool = False
    apply: bool = False
    undo: bool = False
    command_env: bool = False

    @staticmethod
    def display_name(field: str) -> str:
        return field.rstrip("_")

    def missing(self, required: BackendCapabilities) -> list[str]:
        """Display names of capabilities ``required`` asks for that ``self`` lacks."""
        return [
            self.display_name(name)
            for name in type(self).model_fields
            if getattr(required, name) and not getattr(self, name)
        ]


class SkippedFolder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    reason: str


class ImportDecision(BaseModel):
    """One album decision, reported while an import runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    outcome: Literal["imported", "skipped", "duplicate"]
    reason: str | None = None


class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    imported: list[AlbumSnapshot] = Field(default_factory=list[AlbumSnapshot])
    skipped: list[SkippedFolder] = Field(default_factory=list[SkippedFolder])
    duplicates: list[SkippedFolder] = Field(
        default_factory=list[SkippedFolder],
        description="Albums that matched one already in the library; reason = what was done. "
        "A duplicate that was kept is also in imported, one that was skipped also in skipped.",
    )


@runtime_checkable
class LibraryBackend(Protocol):
    """The engine's only view of a music library (architecture §8, BACKEND-1).

    Condition and template syntax belong to the backend: beets query and
    template syntax under BeetsBackend.
    """

    capabilities: BackendCapabilities

    def query(self, selection: Selection) -> list[AlbumSnapshot]: ...

    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str: ...

    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool: ...

    def check(self, changes: ChangeSet) -> list[ChangeProblem]:
        """Problems that stop ``changes`` from being applied: unknown targets, read-only
        fields, values the field can't store (CHANGESET-2). Empty when it can apply."""
        ...

    def apply(self, changes: ChangeSet) -> ApplyResult:
        """Apply all of ``changes`` or nothing (CHANGESET-8).

        Every field's ``old`` is compared with its current value first; on any
        difference nothing is written and the conflicts are returned. Raises
        ``ChangeSetRejected`` if ``check`` finds problems. Only the engine calls this,
        after journaling the intent (CHANGESET-13, JOURNAL-1).
        """
        ...

    def import_folder(
        self,
        path: Path,
        mode: ImportMode,
        decide: DecisionCallback | None,
        *,
        run_id: str | None = None,
        options: Mapping[str, str | bool] | None = None,
        set_fields: Mapping[str, str] | None = None,
        should_stop: Callable[[], bool] | None = None,
        on_decision: Callable[[ImportDecision], None] | None = None,
    ) -> ImportResult:
        """Import ``path``; imported albums get ``RUN_MARKER = run_id`` when given.

        ``options`` override backend importer settings for this import only.
        ``set_fields`` are stored on every imported album and its items (plain strings).
        ``should_stop`` is polled between albums; when it returns True the import stops
        early and returns what was done so far. ``on_decision`` may be called from
        another thread.
        """
        ...

    def command_env(self) -> Mapping[str, str]:
        """Environment for CLI subprocess steps (e.g. ``BEETSDIR``)."""
        ...
