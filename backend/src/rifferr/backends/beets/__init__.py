"""BeetsBackend: the profile's beets library, used in-process inside the worker.

The only package that may import beets (AGENTS.md, BEETS-IMPORT-1). Library
handles are opened per call and closed afterwards (BEETS-IMPORT-14).
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from beets import dbcore
from beets.library import Album, Item, Library
from beets.library.queries import parse_query_parts

from rifferr.backends.base import (
    RUN_MARKER,
    BackendCapabilities,
    CapabilityError,
    ChangeSetRejected,
    DecisionCallback,
    ImportDecision,
    ImportMode,
    ImportResult,
    QueryError,
    SharedFields,
    SkippedFolder,
    TemplateError,
)
from rifferr.backends.beets import apply as changes_apply
from rifferr.backends.beets import config
from rifferr.backends.beets.importer import QuietImportSession
from rifferr.backends.beets.snapshots import album_snapshot, field_value, parse_id
from rifferr.backends.coerce import coerce_value
from rifferr.core.changeset import ApplyResult, ChangeProblem, ChangeSet, same_value
from rifferr.core.ids import new_ulid
from rifferr.core.profile import Profile
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

CAPABILITIES = BackendCapabilities(
    import_=True, query=True, templates=True, apply=True, command_env=True
)


READ_ONLY_CAPABILITIES = CAPABILITIES.model_copy(update={"import_": False, "apply": False})


def from_profile(profile: Profile, *, read_only: bool = False) -> BeetsBackend:
    """``rifferr.backends`` entry point (BEETS-IMPORT-15, BACKEND-13)."""
    beetsdir = profile.require_beetsdir()
    config.load(beetsdir)
    return BeetsBackend(
        Path(beetsdir).expanduser().resolve(),
        write_tags=profile.changes.write_tags,
        read_only=read_only,
    )


class _QueryOnlyLibrary(Library):
    """A Library whose connections refuse every write (``PRAGMA query_only``)."""

    def _create_connection(self) -> Any:
        conn = super()._create_connection()
        conn.execute("PRAGMA query_only = ON")
        return conn


_TEMPLATE_SPECIALS = re.compile(r"([$%},])")


def escape_template(value: str) -> str:
    """Make ``value`` literal in a beets template: ``$``, ``%``, ``}`` and ``,`` get a
    ``$`` escape (``{`` has none and needs none once ``}`` is escaped)."""
    return _TEMPLATE_SPECIALS.sub(r"$\1", value)


class BeetsBackend:
    def __init__(self, beetsdir: Path, *, write_tags: bool = True, read_only: bool = False) -> None:
        if config.loaded_beetsdir() != beetsdir:
            raise RuntimeError(f"call config.load({beetsdir}) first")
        self.beetsdir = beetsdir
        self.write_tags = write_tags
        """Write changed tags to files after applying (profile ``changes.write_tags``)."""
        self.read_only = read_only
        """Opened read-only (BACKEND-13): no apply or import, and SQLite refuses writes."""
        self.capabilities = READ_ONLY_CAPABILITIES if read_only else CAPABILITIES

    @contextlib.contextmanager
    def library(self) -> Iterator[Library]:
        """Open the library for one call; never held between calls (BEETS-IMPORT-14)."""
        db, directory = config.library_paths()
        if not db.parent.is_dir():
            raise QueryError(f"beets library directory {db.parent} does not exist")
        try:
            lib = (_QueryOnlyLibrary if self.read_only else Library)(str(db), str(directory))
        except sqlite3.OperationalError as exc:  # read-only and not set up yet
            raise QueryError(f"can't open the beets library {db} read-only: {exc}") from exc
        try:
            yield lib
        finally:
            lib._close()  # pyright: ignore[reportPrivateUsage]

    # -- reads -----------------------------------------------------------------

    def query(self, selection: Selection) -> list[AlbumSnapshot]:
        parts = _split(selection.query)
        with self.library() as lib:
            allowed: set[int] | None = None
            if selection.run_id is not None:
                marker = _run_term(selection.run_id)
                allowed = {a.id for a in _fetch(lambda: lib.albums([marker])) if a.id is not None}
            if selection.scope == "album":
                albums = _fetch(lambda: lib.albums(parts))
                return [album_snapshot(a) for a in albums if allowed is None or a.id in allowed]
            by_album: dict[int, list[Item]] = {}
            for item in _fetch(lambda: lib.items(parts)):
                if item.album_id is not None and (allowed is None or item.album_id in allowed):
                    by_album.setdefault(item.album_id, []).append(item)
            out: list[AlbumSnapshot] = []
            for album in _fetch(lambda: lib.albums()):
                if album.id in by_album:
                    out.append(album_snapshot(album, by_album[album.id]))
            return out

    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool:
        """On the target's values, over the stored object (BACKEND-10)."""
        with self.library() as lib:
            obj = _overlay(_load(lib, target.id), target)
            query, _ = _parse(_split(condition), type(obj))
            return bool(query.match(obj))

    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str:
        """On the target's values, over the stored object (BACKEND-10)."""
        with self.library() as lib:
            obj = _overlay(_load(lib, target.id), target)
            try:
                return obj.evaluate_template(template)
            except Exception as exc:  # beets raises various errors for broken templates
                raise TemplateError(f"template {template!r}: {exc}") from exc

    def coerce(self, field: str, value: FieldValue) -> FieldValue:
        """By the field's beets type; flexible attributes without a type stay
        strings (BACKEND-11)."""
        model = Album if field in Album._fields and field not in Item._fields else Item
        kind: Any = model._type(field)  # pyright: ignore[reportPrivateUsage]
        wanted: Any = kind.model_type
        if wanted not in (int, float, bool, str, list):
            raise ValueError(f"field type {getattr(wanted, '__name__', wanted)} can't be set")
        empty = kind.null if kind.null is not None else None
        return coerce_value(
            wanted, value, empty=field_value(empty), split=lambda t: list(kind.parse(t))
        )

    def album_item_fields(self) -> SharedFields:
        """``Album.item_keys`` and every flexible attribute (beets' ``Album.store``)."""
        fixed = frozenset(Album._fields)  # pyright: ignore[reportPrivateUsage]
        return SharedFields(exclude=fixed - frozenset(Album.item_keys))

    def command_env(self) -> Mapping[str, str]:
        return {"BEETSDIR": str(self.beetsdir)}

    def check(self, changes: ChangeSet) -> list[ChangeProblem]:
        with self.library() as lib:
            return changes_apply.check(lib, changes)

    def _writable(self, what: str) -> None:
        if self.read_only:
            raise CapabilityError(f"this beets library is open read-only; can't {what}")

    def apply(self, changes: ChangeSet) -> ApplyResult:
        self._writable("apply changes")
        with self.library() as lib:
            problems = changes_apply.check(lib, changes)
            if problems:
                raise ChangeSetRejected(problems)
            return changes_apply.apply(lib, changes, write_tags=self.write_tags)

    # -- import ----------------------------------------------------------------

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
        """Quiet import. Without ``run_id`` a fresh id still marks the albums, so the
        result can list them."""
        self._writable("import")
        if mode != "quiet":
            raise NotImplementedError(f"import mode '{mode}' arrives with F-IMPORT-UI (M2)")
        marker = run_id or new_ulid()
        fields = {k: escape_template(v) for k, v in (set_fields or {}).items()}
        fields[RUN_MARKER] = escape_template(marker)
        report = on_decision or (lambda d: None)
        settings: dict[str, Any] = {
            **dict(options or {}),
            "quiet": True,
            "threaded": False,  # decisions and stop checks happen one album at a time
            "set_fields": fields,
        }
        with self.library() as lib, config.overlay({"import": settings}):
            before = {a.id for a in lib.albums([_run_term(marker)])}
            session = QuietImportSession(
                lib, os.fsencode(path), should_stop=should_stop or (lambda: False), report=report
            )
            session.run()
            imported = [
                album_snapshot(a) for a in lib.albums([_run_term(marker)]) if a.id not in before
            ]
        for album in imported:
            report(ImportDecision(path=album.path or str(path), outcome="imported"))
        return ImportResult(
            imported=imported,
            skipped=[SkippedFolder(path=d.path, reason=d.reason or "") for d in session.skipped],
            duplicates=[
                SkippedFolder(path=d.path, reason=d.reason or "") for d in session.duplicates
            ],
        )


# -- helpers ------------------------------------------------------------------


def _split(query: str) -> list[str]:
    try:
        return shlex.split(query)  # beets' own query string rule
    except ValueError as exc:
        raise QueryError(f"invalid query {query!r}: {exc}") from exc


def _run_term(run_id: str) -> str:
    return f"{RUN_MARKER}::^{re.escape(run_id)}$"


def _parse(parts: list[str], model: type[Item] | type[Album]) -> tuple[Any, Any]:
    try:
        return parse_query_parts(parts, model)
    except dbcore.InvalidQueryError as exc:
        raise QueryError(str(exc)) from exc


def _fetch[T](fetch: Callable[[], T]) -> T:
    try:
        return fetch()
    except dbcore.InvalidQueryError as exc:
        raise QueryError(str(exc)) from exc


def _overlay(obj: Item | Album, target: ItemSnapshot | AlbumSnapshot) -> Item | Album:
    """``obj`` with the snapshot's values, in memory only (never stored).
    ``None`` removes a flexible attribute (BACKEND-10)."""
    fixed = type(obj)._fields  # pyright: ignore[reportPrivateUsage]
    for key, value in target.fields.items():
        if key in ("id", "path"):
            continue
        if value is None and key not in fixed:
            with contextlib.suppress(KeyError):  # an album field an item only inherits
                del obj[key]
        elif not same_value(field_value(obj.get(key)), value):
            obj[key] = value
    return obj


def _load(lib: Library, snapshot_id: str) -> Item | Album:
    kind, number = parse_id(snapshot_id)
    obj = lib.get_album(number) if kind == "album" else lib.get_item(number)
    if obj is None:
        raise KeyError(f"{snapshot_id} is no longer in the library")
    return obj


__all__ = ["BeetsBackend", "escape_template", "from_profile"]
