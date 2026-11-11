"""FakeBackend: an in-memory LibraryBackend for engine and step tests (BACKEND-6, -7).

It reads no files and imports nothing real. Plugin authors can use it to test
their own steps.

Supported query subset (beets semantics), space-separated terms are ANDed:

- ``field:value``  string fields: case-insensitive substring; list fields: any
  element; int/float fields: exact number or ``a..b`` range; bool fields:
  ``true``/``false`` (also ``1``/``0``, ``yes``/``no``)
- ``field::regex`` Python regex search, case-sensitive
- ``^term``        negation
- quoting with shell rules: ``'album:First EP'``

A term for a field the target doesn't have never matches (so ``^term`` does).
Bare terms without a field and beets' other query types are rejected with
``QueryError`` rather than approximated.

Templates support ``$field``, ``${field}``, ``$$`` and ``$path``. Unknown fields
are left as written, like beets. Item templates also see their album's fields.
Template functions (``%func{}``) raise ``TemplateError``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rifferr.backends.base import (
    RUN_MARKER,
    BackendCapabilities,
    CapabilityError,
    DecisionCallback,
    ImportMode,
    ImportResult,
    QueryError,
    SkippedFolder,
    TemplateError,
)
from rifferr.core.changeset import ChangeSet, JournalEntry
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

DEFAULT_CAPABILITIES = BackendCapabilities(
    import_=True, query=True, templates=True, command_env=True
)
TRUE_WORDS = {"1", "true", "t", "yes", "y", "on"}
FALSE_WORDS = {"0", "false", "f", "no", "n", "off"}


@dataclass
class _Item:
    id: str
    path: str
    fields: dict[str, FieldValue]


@dataclass
class _Album:
    id: str
    path: str | None
    fields: dict[str, FieldValue]
    items: list[_Item] = field(default_factory=list[_Item])


@dataclass(frozen=True)
class ImportCall:
    path: Path
    mode: ImportMode
    run_id: str | None


@dataclass(frozen=True)
class _Term:
    field: str
    pattern: str
    regex: bool
    negate: bool


class FakeBackend:
    def __init__(
        self,
        capabilities: BackendCapabilities | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.capabilities = capabilities or DEFAULT_CAPABILITIES
        self._env = dict(env or {})
        self._albums: list[_Album] = []
        self._next_id = 0
        self._import_fixtures: dict[str, list[dict[str, Any]] | str] = {}
        self._imported: set[str] = set()
        self.import_calls: list[ImportCall] = []

    # -- building ------------------------------------------------------------

    @classmethod
    def from_fixture(
        cls,
        data: Mapping[str, Any],
        capabilities: BackendCapabilities | None = None,
        env: Mapping[str, str] | None = None,
    ) -> FakeBackend:
        """``{"albums": [{<album fields>, "path"?: ..., "items": [{<item fields>}]}]}``."""
        fb = cls(capabilities, env)
        for album in data.get("albums", []):
            fb.add_album(album)
        return fb

    def add_album(self, spec: Mapping[str, Any]) -> AlbumSnapshot:
        fields = {k: v for k, v in spec.items() if k not in ("items", "path")}
        path = (
            spec.get("path") or f"/fake/{fields.get('albumartist', '_')}/{fields.get('album', '_')}"
        )
        album = _Album(self._new_id("album"), str(path), dict(fields))
        for item_spec in spec.get("items", []):
            item_fields = {k: v for k, v in item_spec.items() if k != "path"}
            track = item_fields.get("track")
            name = f"{track:02d} " if isinstance(track, int) else ""
            item_path = (
                item_spec.get("path") or f"{path}/{name}{item_fields.get('title', 'track')}.flac"
            )
            album.items.append(_Item(self._new_id("item"), str(item_path), dict(item_fields)))
        self._albums.append(album)
        return _album_snapshot(album)

    def add_import_fixture(
        self,
        path: Path,
        albums: Sequence[Mapping[str, Any]] | None = None,
        *,
        skip_reason: str | None = None,
    ) -> None:
        """Declare what importing ``path`` produces: albums, or a skip with a reason."""
        if skip_reason is not None:
            self._import_fixtures[str(path)] = skip_reason
        else:
            self._import_fixtures[str(path)] = [dict(a) for a in albums or []]

    def _new_id(self, kind: str) -> str:
        self._next_id += 1
        return f"{kind}-{self._next_id}"

    def _need(self, capability: str) -> None:
        if not getattr(self.capabilities, capability):
            name = BackendCapabilities.display_name(capability)
            raise CapabilityError(f"FakeBackend configured without capability '{name}'")

    # -- protocol ------------------------------------------------------------

    def query(self, selection: Selection) -> list[AlbumSnapshot]:
        self._need("query")
        terms = parse_query(selection.query)
        out: list[AlbumSnapshot] = []
        for album in self._albums:
            if selection.run_id is not None and album.fields.get(RUN_MARKER) != selection.run_id:
                continue
            if selection.scope == "album":
                if _matches_all(terms, _album_env(album)):
                    out.append(_album_snapshot(album))
            else:
                items = [i for i in album.items if _matches_all(terms, _item_env(i))]
                if items:
                    out.append(_album_snapshot(album, items))
        return out

    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool:
        self._need("query")
        terms = parse_query(condition)
        env = _snapshot_env(target, None)
        return _matches_all(terms, env)

    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str:
        self._need("templates")
        album_fields = self._album_fields_for(target)
        return render_template(template, _snapshot_env(target, album_fields))

    def apply(self, changes: ChangeSet) -> JournalEntry:
        self._need("apply")
        raise NotImplementedError("ChangeSets arrive with C-CHANGESET (M1)")

    def import_folder(
        self,
        path: Path,
        mode: ImportMode,
        decide: DecisionCallback | None,
        *,
        run_id: str | None = None,
    ) -> ImportResult:
        self._need("import_")
        if mode != "quiet":
            raise NotImplementedError(f"import mode '{mode}' arrives with F-IMPORT-UI (M2)")
        self.import_calls.append(ImportCall(path, mode, run_id))
        key = str(path)
        fixture = self._import_fixtures.get(key)
        if key in self._imported:
            return ImportResult(skipped=[SkippedFolder(path=key, reason="already imported")])
        if fixture is None:
            return ImportResult(
                skipped=[SkippedFolder(path=key, reason="no import fixture registered")]
            )
        if isinstance(fixture, str):
            return ImportResult(skipped=[SkippedFolder(path=key, reason=fixture)])
        imported: list[AlbumSnapshot] = []
        for spec in fixture:
            spec = {"path": key, **spec}
            if run_id is not None:
                spec[RUN_MARKER] = run_id
            imported.append(self.add_album(spec))
        self._imported.add(key)
        return ImportResult(imported=imported)

    def command_env(self) -> Mapping[str, str]:
        self._need("command_env")
        return dict(self._env)

    # -- helpers -------------------------------------------------------------

    def _album_fields_for(self, target: ItemSnapshot | AlbumSnapshot) -> dict[str, FieldValue]:
        if isinstance(target, AlbumSnapshot):
            return {}
        for album in self._albums:
            if any(i.id == target.id for i in album.items):
                return album.fields
        return {}


# -- snapshots ----------------------------------------------------------------


def _sort_key(item: _Item) -> tuple[int, int]:
    def num(v: FieldValue) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    return (num(item.fields.get("disc")), num(item.fields.get("track")))


def _album_snapshot(album: _Album, items: list[_Item] | None = None) -> AlbumSnapshot:
    chosen = sorted(album.items if items is None else items, key=_sort_key)
    return AlbumSnapshot(
        id=album.id,
        path=album.path,
        fields=dict(album.fields),
        items=[ItemSnapshot(id=i.id, path=i.path, fields=dict(i.fields)) for i in chosen],
    )


def _album_env(album: _Album) -> dict[str, FieldValue]:
    return {**album.fields, "path": album.path}


def _item_env(item: _Item) -> dict[str, FieldValue]:
    return {**item.fields, "path": item.path}


def _snapshot_env(
    target: ItemSnapshot | AlbumSnapshot, album_fields: Mapping[str, FieldValue] | None
) -> dict[str, FieldValue]:
    return {**(album_fields or {}), **target.fields, "path": target.path}


# -- queries ------------------------------------------------------------------

_TERM = re.compile(r"^(?P<field>[A-Za-z_][A-Za-z0-9_]*)(?P<sep>::?)(?P<pattern>.*)$", re.S)


def parse_query(query: str) -> list[_Term]:
    try:
        tokens = shlex.split(query)
    except ValueError as exc:
        raise QueryError(f"invalid query {query!r}: {exc}") from exc
    terms: list[_Term] = []
    for token in tokens:
        negate = token.startswith("^")
        body = token[1:] if negate else token
        m = _TERM.match(body)
        if m is None:
            raise QueryError(
                f"FakeBackend supports only 'field:value' and 'field::regex' terms, got {token!r}"
            )
        regex = m["sep"] == "::"
        if regex:
            try:
                re.compile(m["pattern"])
            except re.error as exc:
                raise QueryError(f"invalid regex in {token!r}: {exc}") from exc
        terms.append(_Term(m["field"], m["pattern"], regex, negate))
    return terms


def _matches_all(terms: list[_Term], env: Mapping[str, FieldValue]) -> bool:
    return all(_term_matches(t, env) != t.negate for t in terms)


def _term_matches(term: _Term, env: Mapping[str, FieldValue]) -> bool:
    value = env.get(term.field)
    if value is None:
        return False
    if term.regex:
        values = value if isinstance(value, list) else [_render(value)]
        return any(re.search(term.pattern, v) for v in values)
    if isinstance(value, bool):
        word = term.pattern.lower()
        if word in TRUE_WORDS:
            return value
        if word in FALSE_WORDS:
            return not value
        return False
    if isinstance(value, int | float):
        return _numeric_match(term, value)
    needle = term.pattern.lower()
    if isinstance(value, list):
        return any(needle in v.lower() for v in value)
    return needle in value.lower()


def _numeric_match(term: _Term, value: float) -> bool:
    try:
        if ".." in term.pattern:
            low, high = term.pattern.split("..", 1)
            return (not low or value >= float(low)) and (not high or value <= float(high))
        return value == float(term.pattern)
    except ValueError as exc:
        raise QueryError(f"{term.field}: {term.pattern!r} is not a number") from exc


# -- templates ----------------------------------------------------------------

_TEMPLATE = re.compile(r"\$\$|\$\{(?P<braced>[A-Za-z0-9_]+)\}|\$(?P<bare>[A-Za-z0-9_]+)")
_FUNCTION = re.compile(r"%[A-Za-z0-9_]+\{")


def render_template(template: str, env: Mapping[str, FieldValue]) -> str:
    if _FUNCTION.search(template):
        raise TemplateError("FakeBackend doesn't support template functions (%func{...})")

    def sub(m: re.Match[str]) -> str:
        if m.group(0) == "$$":
            return "$"
        name = m["braced"] or m["bare"]
        if name not in env:
            return m.group(0)
        return _render(env[name])

    return _TEMPLATE.sub(sub, template)


def _render(value: FieldValue) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "; ".join(value)
    return str(value)
