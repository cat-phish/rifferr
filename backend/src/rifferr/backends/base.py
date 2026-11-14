"""LibraryBackend protocol and capabilities (C-BACKEND).

The engine, steps, UI, covers and AI see a music library only through this
protocol and the snapshot models in ``rifferr.core``. Implementations register
under the ``rifferr.backends`` entry-point group.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from rifferr.core.changeset import ApplyResult, ChangeProblem, ChangeSet
from rifferr.core.imports import ImportAction, ImportQuestion
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

#: Flexible attribute holding the id of the run that imported or last touched an album.
RUN_MARKER: Final = "rifferr_run"

ImportMode = Literal["quiet", "auto", "interactive"]

#: Reserved. Import questions come back in ``ImportResult.questions`` instead of through
#: a callback, because the answer may take days (F-IMPORT-UI); pass ``None``.
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


@dataclass(frozen=True)
class SharedFields:
    """Album fields a backend copies to the album's items when the album is stored
    (BACKEND-12, TAGSCRIPT-3). ``include=None`` means every field not in ``exclude``
    (beets: fixed ``Album.item_keys`` and all flexible attributes)."""

    include: frozenset[str] | None = None
    exclude: frozenset[str] = frozenset()

    def __contains__(self, field: object) -> bool:
        if not isinstance(field, str) or field in self.exclude:
            return False
        return self.include is None or field in self.include


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
    cli: bool = False
    """The backend has a command-line tool steps can run (BACKEND-14)."""
    import_decisions: bool = False
    """``import_folder`` can ask about albums (modes ``auto``/``interactive``), and
    ``import_decided``/``lookup`` answer them (F-IMPORT-UI)."""

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


class ImportOutcome(BaseModel):
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
    questions: list[ImportQuestion] = Field(
        default_factory=list[ImportQuestion],
        description="Albums left in place for an import decision (F-IMPORT-UI IMPORT-UI-4)",
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

    # ``evaluate`` and ``matches`` use the field values in ``target``, not the stored
    # ones; fields absent from it fall back to the stored object, and a field given
    # as ``None`` counts as absent (BACKEND-10).

    def coerce(self, field: str, value: FieldValue) -> FieldValue:
        """``value`` converted to ``field``'s type (BACKEND-11). Strings are parsed;
        ``''`` is the type's empty value. Lists pass through unchanged (``check``
        rejects them for single-value fields). Raises ``ValueError`` if it can't."""
        ...

    def album_item_fields(self) -> SharedFields:
        """Album fields carried to the album's items on store (BACKEND-12)."""
        ...

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
        on_decision: Callable[[ImportOutcome], None] | None = None,
    ) -> ImportResult:
        """Import ``path``; imported albums get ``RUN_MARKER = run_id`` when given.

        ``options`` override backend importer settings for this import only.
        ``set_fields`` are stored on every imported album and its items (plain strings).
        ``should_stop`` is polled between albums; when it returns True the import stops
        early and returns what was done so far. ``on_decision`` may be called from
        another thread.
        """
        ...

    def import_decided(
        self,
        path: Path,
        action: ImportAction,
        *,
        fingerprint: str,
        candidate_id: str | None = None,
        run_id: str | None = None,
        options: Mapping[str, str | bool] | None = None,
        set_fields: Mapping[str, str] | None = None,
    ) -> ImportResult:
        """Import the one album folder ``path`` as decided (IMPORT-UI-11).

        ``candidate`` applies the candidate with ``candidate_id`` (looked up again by its
        id), ``asis`` keeps the current tags, ``skip`` imports nothing, and ``keep`` /
        ``remove`` / ``merge`` answer a duplicate. Raises ``ImportApplyError`` when the
        folder no longer has ``fingerprint``, or the candidate isn't found again.
        """
        ...

    def lookup(
        self,
        path: Path,
        *,
        ids: Sequence[str] = (),
        artist: str | None = None,
        album: str | None = None,
    ) -> ImportQuestion:
        """Look album folder ``path`` up again (IMPORT-UI-9): its current fingerprint and
        items, and candidates by ``ids``, by ``artist``/``album``, or from a plain lookup
        when neither is given. Raises ``ImportApplyError`` when it can't."""
        ...

    def command_env(self) -> Mapping[str, str]:
        """Environment for CLI subprocess steps (e.g. ``BEETSDIR``)."""
        ...

    def cli_command(self, subcommand: str, args: list[str], selection: Selection) -> list[str]:
        """The argv that runs ``subcommand`` of the backend's command-line tool on
        ``selection`` (BACKEND-14): the tool, ``subcommand``, ``args``, then the selection
        as query arguments, last. Raises ``ValueError`` for a selection that would match
        the whole library."""
        ...
