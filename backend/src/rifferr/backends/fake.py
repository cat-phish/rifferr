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

ChangeSets (CHANGESET-12) follow beets semantics: album-scoped changes are
carried to the album's items for the fields in ``inherited_fields`` (default:
all album fields, as beets 2.x does); ``delete`` removes the field. Fields in
``read_only`` can't be changed, and values must fit ``field_types`` (fields not
listed there take strings only, like an untyped beets flexible attribute).
``fail_writes`` simulates tag-write failures for items, by item id.
"""

from __future__ import annotations

import re
import shlex
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
from rifferr.backends.coerce import FALSE_WORDS, TRUE_WORDS, coerce_value
from rifferr.core.changeset import (
    ApplyResult,
    ChangeProblem,
    ChangeSet,
    FieldEffect,
    TargetRef,
    WriteFailure,
    diff_effects,
    find_conflicts,
)
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot

DEFAULT_CAPABILITIES = BackendCapabilities(
    import_=True, query=True, templates=True, apply=True, command_env=True, cli=True
)
#: Default command-line tool of a FakeBackend: prints its arguments as JSON (BACKEND-14).
ECHO_CLI = (sys.executable, "-c", "import json, sys; print(json.dumps(sys.argv[1:]))")
#: Fields a ChangeSet can't touch (as in beets: ids, paths and bookkeeping).
READ_ONLY_FIELDS = frozenset({"id", "path", "album_id", "mtime", "added"})
_INT, _FLOAT, _BOOL, _STR, _LIST = int, float, bool, str, list
#: Field types for type checks; any other field takes strings only.
DEFAULT_FIELD_TYPES: dict[str, type] = {
    **{
        f: _INT
        for f in (
            "year",
            "month",
            "day",
            "original_year",
            "original_month",
            "original_day",
            "track",
            "tracktotal",
            "disc",
            "disctotal",
            "bpm",
            "length_s",
        )
    },
    **{f: _FLOAT for f in ("rg_track_gain", "rg_album_gain", "length")},
    "comp": _BOOL,
    "genres": _LIST,
    "artists": _LIST,
    "albumartists": _LIST,
}
_EMPTY: dict[type, FieldValue] = {_INT: 0, _FLOAT: 0.0, _BOOL: False, _STR: "", _LIST: []}


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
    options: dict[str, str | bool] = field(default_factory=dict[str, str | bool])
    set_fields: dict[str, str] = field(default_factory=dict[str, str])


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
        *,
        inherited_fields: frozenset[str] | None = None,
        read_only: frozenset[str] = READ_ONLY_FIELDS,
        field_types: Mapping[str, type] | None = None,
        cli: Sequence[str] = ECHO_CLI,
    ) -> None:
        self.capabilities = capabilities or DEFAULT_CAPABILITIES
        self.cli = list(cli)
        """argv prefix ``cli_command`` starts with; tests point it at their own script."""
        self._env = dict(env or {})
        self.inherited_fields = inherited_fields
        """Album fields carried to items on apply; ``None`` = all (beets 2.x)."""
        self.read_only = read_only
        self.field_types = dict(DEFAULT_FIELD_TYPES if field_types is None else field_types)
        self.fail_writes: dict[str, str] = {}
        """item id -> error message, to simulate tag-write failures (CHANGESET-10)."""
        self.apply_calls: list[ChangeSet] = []
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
        **options: Any,
    ) -> FakeBackend:
        """``{"albums": [{<album fields>, "path"?: ..., "items": [{<item fields>}]}]}``.

        ``options`` go to the constructor (``inherited_fields``, ``read_only``, ...).
        Items get the album's inherited fields unless they set them, as in beets.
        """
        fb = cls(capabilities, env, **options)
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
            inherited = {k: v for k, v in fields.items() if self._inherits(k)}
            item_fields = {**inherited, **{k: v for k, v in item_spec.items() if k != "path"}}
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

    def read_only_view(self) -> FakeBackend:
        """The same library, opened read-only (BACKEND-13): no apply, no import."""
        view = type(self)(
            self.capabilities.model_copy(
                update={"apply": False, "import_": False, "undo": False, "cli": False}
            ),
            self._env,
            inherited_fields=self.inherited_fields,
            read_only=self.read_only,
            field_types=self.field_types,
        )
        view._albums = self._albums
        return view

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
        ids = None if selection.ids is None else set(selection.ids)
        for album in self._albums:
            if selection.run_id is not None and album.fields.get(RUN_MARKER) != selection.run_id:
                continue
            if selection.scope == "album":
                if (ids is None or album.id in ids) and _matches_all(terms, _album_env(album)):
                    out.append(_album_snapshot(album))
            else:
                items = [
                    i
                    for i in album.items
                    if (ids is None or i.id in ids) and _matches_all(terms, _item_env(i))
                ]
                if items:
                    out.append(_album_snapshot(album, items))
        return out

    def matches(self, condition: str, target: ItemSnapshot | AlbumSnapshot) -> bool:
        self._need("query")
        terms = parse_query(condition)
        return _matches_all(terms, _snapshot_env(target, None, self._stored_fields(target)))

    def evaluate(self, template: str, target: ItemSnapshot | AlbumSnapshot) -> str:
        self._need("templates")
        album_fields = self._album_fields_for(target)
        env = _snapshot_env(target, album_fields, self._stored_fields(target))
        return render_template(template, env)

    def coerce(self, field: str, value: FieldValue) -> FieldValue:
        """By ``field_types``; multi-value text splits on ``;`` (BACKEND-11)."""
        wanted = self.field_types.get(field, _STR)
        return coerce_value(wanted, value, empty=_EMPTY.get(wanted), split=_split_list)

    def album_item_fields(self) -> SharedFields:
        return SharedFields(include=self.inherited_fields, exclude=frozenset({"id", "path"}))

    def check(self, changes: ChangeSet) -> list[ChangeProblem]:
        self._need("query")  # read only: works on a read-only view too (TAG-GRID-7)
        problems: list[ChangeProblem] = []
        for t in changes.targets:
            if self._find(t.target) is None:
                problems.append(ChangeProblem(target=t.target, field="", message="not found"))
                continue
            for c in t.changes:
                message = self._field_problem(c.field, c.op, c.new)
                if message is not None:
                    problems.append(ChangeProblem(target=t.target, field=c.field, message=message))
        return problems

    def apply(self, changes: ChangeSet) -> ApplyResult:
        self._need("apply")
        self.apply_calls.append(changes)
        problems = self.check(changes)
        if problems:
            raise ChangeSetRejected(problems)
        conflicts = find_conflicts(changes, self._current_fields)
        if conflicts:
            return ApplyResult(applied=False, conflicts=conflicts)
        touched = self._touched_albums(changes)
        before = {ref: dict(fields) for ref, fields in self._all_fields(touched)}
        for t in changes.targets:
            obj = self._find(t.target)
            assert obj is not None
            for c in t.changes:
                self._set(obj, c.field, c.new, delete=c.op == "delete")
                if isinstance(obj, _Album) and self._inherits(c.field):
                    for item in obj.items:
                        self._set(item, c.field, c.new, delete=c.op == "delete")
        effects: list[FieldEffect] = []
        for ref, fields in self._all_fields(touched):
            effects += diff_effects(ref, before[ref], fields)
        changed_items = {e.target.id for e in effects if e.target.scope == "item"}
        failures = [
            WriteFailure(target=TargetRef(scope="item", id=i.id), path=i.path, error=error)
            for a in touched
            for i in a.items
            if i.id in changed_items and (error := self.fail_writes.get(i.id)) is not None
        ]
        return ApplyResult(applied=True, effects=effects, write_failures=failures)

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
        """``options`` are recorded, not interpreted."""
        self._need("import_")
        if mode != "quiet":
            raise NotImplementedError(f"import mode '{mode}' arrives with F-IMPORT-UI (M2)")
        self.import_calls.append(
            ImportCall(path, mode, run_id, dict(options or {}), dict(set_fields or {}))
        )
        report = on_decision or (lambda d: None)
        key = str(path)
        fixture = self._import_fixtures.get(key)
        reason = None
        if key in self._imported:
            reason = "already imported"
        elif fixture is None:
            reason = "no import fixture registered"
        elif isinstance(fixture, str):
            reason = fixture
        if reason is not None or not isinstance(fixture, list):
            reason = reason or "skipped"
            report(ImportDecision(path=key, outcome="skipped", reason=reason))
            return ImportResult(skipped=[SkippedFolder(path=key, reason=reason)])
        imported: list[AlbumSnapshot] = []
        for spec in fixture:
            if should_stop is not None and should_stop():
                break
            spec = {"path": key, **spec, **(set_fields or {})}
            if run_id is not None:
                spec[RUN_MARKER] = run_id
            album = self.add_album(spec)
            imported.append(album)
            report(ImportDecision(path=album.path or key, outcome="imported"))
        self._imported.add(key)
        return ImportResult(imported=imported)

    def cli_command(self, subcommand: str, args: list[str], selection: Selection) -> list[str]:
        """``self.cli``, then the subcommand, args and query parts (BACKEND-14)."""
        self._need("cli")
        query = shlex.split(selection.query) if selection.query else []
        if selection.run_id is not None:
            query.insert(0, f"{RUN_MARKER}::^{re.escape(selection.run_id)}$")
        if not query:
            raise ValueError("an empty selection would run on the whole library")
        return [*self.cli, subcommand, *args, *query]

    def command_env(self) -> Mapping[str, str]:
        self._need("command_env")
        return dict(self._env)

    # -- helpers -------------------------------------------------------------

    def _find(self, ref: TargetRef) -> _Album | _Item | None:
        for album in self._albums:
            if ref.scope == "album" and album.id == ref.id:
                return album
            if ref.scope == "item":
                for item in album.items:
                    if item.id == ref.id:
                        return item
        return None

    def _current_fields(self, ref: TargetRef) -> dict[str, FieldValue] | None:
        obj = self._find(ref)
        return None if obj is None else obj.fields

    def _touched_albums(self, changes: ChangeSet) -> list[_Album]:
        ids = {t.target.id for t in changes.targets}
        return [a for a in self._albums if a.id in ids or any(i.id in ids for i in a.items)]

    @staticmethod
    def _all_fields(albums: list[_Album]) -> list[tuple[TargetRef, dict[str, FieldValue]]]:
        out: list[tuple[TargetRef, dict[str, FieldValue]]] = []
        for album in albums:
            out.append((TargetRef(scope="album", id=album.id), album.fields))
            out += [(TargetRef(scope="item", id=i.id), i.fields) for i in album.items]
        return out

    def _inherits(self, name: str) -> bool:
        return self.inherited_fields is None or name in self.inherited_fields

    @staticmethod
    def _set(obj: _Album | _Item, name: str, value: FieldValue, *, delete: bool) -> None:
        if delete:
            obj.fields.pop(name, None)
        else:
            obj.fields[name] = list(value) if isinstance(value, list) else value

    def _field_problem(self, name: str, op: str, value: FieldValue) -> str | None:
        if name in self.read_only:
            return "read-only field"
        if op == "delete":
            return None
        wanted = self.field_types.get(name, _STR)
        ok = {
            _INT: isinstance(value, int) and not isinstance(value, bool),
            _FLOAT: isinstance(value, int | float) and not isinstance(value, bool),
            _BOOL: isinstance(value, bool),
            _LIST: isinstance(value, list),
        }.get(wanted, isinstance(value, str))
        return None if ok else f"expects {wanted.__name__}, got {type(value).__name__}"

    def _stored_fields(self, target: ItemSnapshot | AlbumSnapshot) -> dict[str, FieldValue]:
        scope = "album" if isinstance(target, AlbumSnapshot) else "item"
        obj = self._find(TargetRef(scope=scope, id=target.id))
        return {} if obj is None else obj.fields

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
    target: ItemSnapshot | AlbumSnapshot,
    album_fields: Mapping[str, FieldValue] | None,
    stored: Mapping[str, FieldValue] | None = None,
) -> dict[str, FieldValue]:
    """The target's values over its stored ones and the stored album's; ``None`` =
    absent (BACKEND-10)."""
    env = {**(album_fields or {}), **(stored or {}), **target.fields, "path": target.path}
    return {k: v for k, v in env.items() if v is not None}


def _split_list(text: str) -> list[str]:
    return [p.strip() for p in text.split(";") if p.strip()]


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
