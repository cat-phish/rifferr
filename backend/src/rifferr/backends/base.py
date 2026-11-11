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

from rifferr.core.changeset import ChangeSet, JournalEntry
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


class ImportResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    imported: list[AlbumSnapshot] = Field(default_factory=list[AlbumSnapshot])
    skipped: list[SkippedFolder] = Field(default_factory=list[SkippedFolder])


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

    def apply(self, changes: ChangeSet) -> JournalEntry: ...

    def import_folder(
        self,
        path: Path,
        mode: ImportMode,
        decide: DecisionCallback | None,
        *,
        run_id: str | None = None,
    ) -> ImportResult:
        """Import ``path``; imported albums get ``RUN_MARKER = run_id`` when given."""
        ...

    def command_env(self) -> Mapping[str, str]:
        """Environment for CLI subprocess steps (e.g. ``BEETSDIR``)."""
        ...
