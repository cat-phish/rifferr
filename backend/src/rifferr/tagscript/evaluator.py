"""Tag-script evaluation (S-TAGSCRIPT TAGSCRIPT-3 to -16). Pure.

``run_script`` runs a script's actions in order on one album and its items,
keeping *working values*: each action sees what the earlier ones left
(TAGSCRIPT-3). Templates, queries and value types come from an ``Evaluator``,
which the backend implements. Nothing here writes anything: the result is the
net difference between the snapshot and the working values (TAGSCRIPT-14).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Container
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Protocol

from rifferr.core.changeset import TargetRef, same_value
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot
from rifferr.tagscript.models import (
    PLACEHOLDER,
    ActionBase,
    AppendUniqueAction,
    CaseAction,
    CaseStyle,
    CopyAction,
    CopySpec,
    DeleteAction,
    FromFilenameAction,
    JoinAction,
    MoveAction,
    ReplaceAction,
    ReplaceSpec,
    SetAction,
    SplitAction,
    TagScript,
    action_label,
    effective_scope,
)

Snapshot = ItemSnapshot | AlbumSnapshot
Fields = dict[str, FieldValue]


class Evaluator(Protocol):
    """What the evaluator needs from a library backend (BACKEND-10 to -12)."""

    def evaluate(self, template: str, target: Snapshot) -> str: ...

    def matches(self, condition: str, target: Snapshot) -> bool: ...

    def coerce(self, field: str, value: FieldValue) -> FieldValue: ...

    def album_item_fields(self) -> Container[str]: ...


@dataclass(frozen=True)
class ScriptError:
    """A template, query or type error; the album gets no changes (TAGSCRIPT-15)."""

    action_index: int
    action: str
    target: TargetRef
    field: str | None
    message: str

    def __str__(self) -> str:
        on = f"{self.target.id} {self.field}" if self.field else self.target.id
        return f"{self.action} (#{self.action_index + 1}) on {on}: {self.message}"


@dataclass(frozen=True)
class ScriptNote:
    """Something preview should show that isn't an error (e.g. no filename match)."""

    action_index: int
    action: str
    target: TargetRef
    message: str


@dataclass(frozen=True)
class FieldDiff:
    target: TargetRef
    field: str
    before: FieldValue
    after: FieldValue
    """``None`` when the field is deleted (``deleted``)."""
    deleted: bool
    actions: list[int]
    """Indexes of the actions that changed this field, in order."""


@dataclass(frozen=True)
class IdempotenceIssue:
    """A field a second pass would change, and the action that does it (TAGSCRIPT-16)."""

    target: TargetRef
    field: str
    action_index: int | None
    action: str | None
    first: FieldValue
    second: FieldValue


@dataclass
class _Target:
    ref: TargetRef
    snapshot: Snapshot
    fields: Fields

    def view(self) -> Snapshot:
        """The snapshot with working values; fields deleted so far are ``None``
        (absent), so the backend doesn't fall back to stored values (BACKEND-10)."""
        gone: Fields = {k: None for k in self.snapshot.fields if k not in self.fields}
        return self.snapshot.model_copy(update={"fields": {**gone, **self.fields}})

    def path(self) -> str:
        return self.snapshot.path or ""


@dataclass
class ScriptResult:
    album: AlbumSnapshot
    script: TagScript
    working: dict[TargetRef, Fields]
    """Working values by target: the album first, then its items in track order."""
    hits: dict[tuple[TargetRef, str], list[int]] = field(
        default_factory=dict[tuple[TargetRef, str], list[int]]
    )
    error: ScriptError | None = None
    notes: list[ScriptNote] = field(default_factory=list[ScriptNote])
    shared: Container[str] = field(default_factory=frozenset[str])

    def label(self, index: int) -> str:
        return action_label(self.script.actions[index], index)

    def originals(self) -> dict[TargetRef, Fields]:
        return _original_fields(self.album)

    def diffs(self, *, with_inherited: bool = False) -> list[FieldDiff]:
        """Net differences from the snapshot (TAGSCRIPT-14), in target order.

        Item changes that only follow an album change of a shared field are left out
        unless ``with_inherited``: storing the album carries them (BACKEND-12).
        """
        out: list[FieldDiff] = []
        originals = self.originals()
        album_ref = _ref(self.album)
        album_diffs: Fields = {}
        for ref, fields in self.working.items():
            before = originals[ref]
            for name in [*before, *(k for k in fields if k not in before)]:
                old, new = before.get(name), fields.get(name)
                if name in fields and name in before and same_value(old, new):
                    continue
                if name not in fields and name not in before:
                    continue
                if (
                    not with_inherited
                    and ref != album_ref
                    and name in album_diffs
                    and same_value(album_diffs[name], new)
                    and name in self.shared
                ):
                    continue
                if ref == album_ref:
                    album_diffs[name] = new
                out.append(
                    FieldDiff(
                        target=ref,
                        field=name,
                        before=old,
                        after=new,
                        deleted=name not in fields,
                        actions=list(self.hits.get((ref, name), [])),
                    )
                )
        return out

    def rationale(self, diff: FieldDiff) -> str:
        return ", ".join(dict.fromkeys(self.label(i) for i in diff.actions))


def _ref(snapshot: Snapshot) -> TargetRef:
    scope = "album" if isinstance(snapshot, AlbumSnapshot) else "item"
    return TargetRef(scope=scope, id=snapshot.id)


def _original_fields(album: AlbumSnapshot) -> dict[TargetRef, Fields]:
    out: dict[TargetRef, Fields] = {}
    out[_ref(album)] = dict(album.fields)
    for item in album.items:
        out[_ref(item)] = dict(item.fields)
    return out


class _ActionError(Exception):
    def __init__(self, field: str | None, message: str) -> None:
        self.field = field
        super().__init__(message)


def run_script(
    script: TagScript,
    album: AlbumSnapshot,
    ev: Evaluator,
    *,
    start: dict[TargetRef, Fields] | None = None,
) -> ScriptResult:
    """Run every action in order on ``album`` and its items (TAGSCRIPT-3, -4).

    ``start``: working values to begin with instead of the snapshot's (the second
    pass of the idempotence check). Stops at the first error (TAGSCRIPT-15).
    """
    begin = start or _original_fields(album)
    album_t = _Target(_ref(album), album, dict(begin[_ref(album)]))
    items = [_Target(_ref(i), i, dict(begin[_ref(i)])) for i in album.items]
    shared = ev.album_item_fields()
    working: dict[TargetRef, Fields] = {}
    for t in [album_t, *items]:
        working[t.ref] = t.fields
    result = ScriptResult(album=album, script=script, working=working, shared=shared)
    for index, action in enumerate(script.actions):
        targets = [album_t] if effective_scope(action, script.scope) == "album" else items
        for target in targets:
            try:
                if action.when is not None and not _matches(ev, action.when, target):
                    continue
                changed = _apply(action, target, ev, result, index)
            except _ActionError as exc:
                result.error = ScriptError(
                    index, action_label(action, index), target.ref, exc.field, str(exc)
                )
                return result
            for name in changed:
                result.hits.setdefault((target.ref, name), []).append(index)
            if target is album_t:
                _inherit(album_t, items, changed, shared, result, index)
    return result


def _matches(ev: Evaluator, condition: str, target: _Target) -> bool:
    try:
        return ev.matches(condition, target.view())
    except Exception as exc:  # the backend's QueryError, or a broken backend
        raise _ActionError(None, f"when {condition!r}: {exc}") from exc


def _inherit(
    album: _Target,
    items: list[_Target],
    changed: list[str],
    shared: Container[str],
    result: ScriptResult,
    index: int,
) -> None:
    """Album-scoped changes of shared fields reach the items too (TAGSCRIPT-3)."""
    for name in changed:
        if name not in shared:
            continue
        for item in items:
            if name in album.fields:
                moved = _put(item.fields, name, album.fields[name])
            else:
                moved = _drop(item.fields, name)
            if moved:
                result.hits.setdefault((item.ref, name), []).append(index)


# -- working-value edits -------------------------------------------------------


def _present(fields: Fields, name: str) -> bool:
    return fields.get(name) is not None


def _put(fields: Fields, name: str, value: FieldValue) -> bool:
    if value is None:
        return _drop(fields, name)
    if name in fields and same_value(fields[name], value):
        return False
    fields[name] = list(value) if isinstance(value, list) else value
    return True


def _drop(fields: Fields, name: str) -> bool:
    if name not in fields:
        return False
    del fields[name]
    return True


def _evaluate(ev: Evaluator, template: str, target: _Target, name: str) -> str:
    try:
        return ev.evaluate(template, target.view())
    except Exception as exc:  # the backend's TemplateError
        raise _ActionError(name, f"template {template!r}: {exc}") from exc


def _coerce(ev: Evaluator, name: str, value: FieldValue) -> FieldValue:
    try:
        return ev.coerce(name, value)
    except Exception as exc:  # ValueError from the backend
        raise _ActionError(name, f"can't store {value!r}: {exc}") from exc


# -- actions -------------------------------------------------------------------


def _apply(
    action: ActionBase, target: _Target, ev: Evaluator, result: ScriptResult, index: int
) -> list[str]:
    """Run one action on one target; return the fields it changed, in order."""
    f = target.fields
    changed: list[str] = []

    def put(name: str, value: FieldValue) -> None:
        if _put(f, name, value) and name not in changed:
            changed.append(name)

    def drop(name: str) -> None:
        if _drop(f, name) and name not in changed:
            changed.append(name)

    if isinstance(action, SetAction):
        for name, value in action.set.items():
            if isinstance(value, str):
                raw: FieldValue = _evaluate(ev, value, target, name)
            elif isinstance(value, list):
                raw = [_evaluate(ev, v, target, name) for v in value]
            else:
                raw = value
            put(name, _coerce(ev, name, raw))
    elif isinstance(action, CopyAction | MoveAction):
        spec: CopySpec = action.copy_ if isinstance(action, CopyAction) else action.move
        if _present(f, spec.from_):
            put(spec.to, _coerce(ev, spec.to, f[spec.from_]))
            if isinstance(action, MoveAction) and spec.from_ != spec.to:
                drop(spec.from_)
    elif isinstance(action, ReplaceAction):
        swap = _replacer(action.replace)
        for name in action.replace.fields:
            value = f.get(name)
            if isinstance(value, str):
                put(name, swap(value))
            elif isinstance(value, list):
                put(name, [swap(v) for v in value])
    elif isinstance(action, SplitAction):
        s = action.split
        value = f.get(s.field)
        if isinstance(value, str | list):
            put(s.into or s.field, split_values(value, s.by, strip=s.strip))
    elif isinstance(action, JoinAction):
        j = action.join
        value = f.get(j.field)
        if isinstance(value, list):
            put(j.into or j.field, j.with_.join(value))
    elif isinstance(action, CaseAction):
        c = action.case
        for name in c.fields:
            value = f.get(name)
            if isinstance(value, str):
                put(name, change_case(value, c.style, c.keep))
            elif isinstance(value, list):
                put(name, [change_case(v, c.style, c.keep) for v in value])
    elif isinstance(action, FromFilenameAction):
        spec_ff = action.from_filename
        found = match_filename(spec_ff.pattern, _source_text(target.path(), spec_ff.source))
        if found is None:
            result.notes.append(
                ScriptNote(
                    index,
                    action_label(action, index),
                    target.ref,
                    f"{spec_ff.source} doesn't match {spec_ff.pattern!r}",
                )
            )
        else:
            for name, text in found.items():
                put(name, _coerce(ev, name, text))
    elif isinstance(action, DeleteAction):
        for name in action.delete:
            drop(name)
    elif isinstance(action, AppendUniqueAction):
        a = action.append_unique
        values = [_evaluate(ev, v, target, a.field) for v in a.values]
        put(a.field, append_unique(f.get(a.field), values))
    else:  # pragma: no cover - the Action union is closed
        raise _ActionError(None, f"unknown action {type(action).__name__}")
    return changed


def _replacer(spec: ReplaceSpec) -> Callable[[str], str]:
    if spec.find is not None:
        find = spec.find
        return lambda text: text.replace(find, spec.with_)
    pattern = re.compile(spec.regex or "", spec.re_flags())
    return lambda text: pattern.sub(spec.with_, text)


def split_values(value: str | list[str], by: str, *, strip: bool) -> list[str]:
    """Split a string, or each element of a list, dropping empty parts (TAGSCRIPT-9)."""
    parts = [p for v in ([value] if isinstance(value, str) else value) for p in v.split(by)]
    if strip:
        parts = [p.strip() for p in parts]
    return [p for p in parts if p]


def append_unique(current: FieldValue, values: list[str]) -> list[str]:
    """``current`` as a list with each new value added unless already present
    (TAGSCRIPT-13). Empty values are not added."""
    if isinstance(current, list):
        out = list(current)
    elif current is None or current == "":
        out = []
    else:
        out = [current if isinstance(current, str) else str(current)]
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


_TOKEN = re.compile(r"(\s+)")
_CORE = re.compile(r"^(\W*)(.*?)(\W*)$", re.S)


def _cased(c: str) -> bool:
    return c.lower() != c.upper()


def _first_upper(word: str) -> str:
    for i, c in enumerate(word):
        if _cased(c):
            return word[:i] + c.upper() + word[i + 1 :]
    return word


def change_case(text: str, style: CaseStyle, keep: list[str]) -> str:
    """Change letter case (TAGSCRIPT-10).

    A word is a run of non-whitespace; ``keep`` words are matched on the word
    without surrounding punctuation, case-insensitively, and written as given.
    ``title`` and ``sentence`` only raise first letters, never lower the rest.
    """
    kept = {w.casefold(): w for w in keep}
    out: list[str] = []
    first = True
    for token in _TOKEN.split(text):
        if not token or token.isspace():
            out.append(token)
            continue
        m = _CORE.match(token)
        assert m is not None  # the pattern matches any string
        lead, core, trail = m.groups()
        if core.casefold() in kept:
            core = kept[core.casefold()]
            first = first and not any(_cased(c) for c in core)
        elif style == "lower":
            core = core.lower()
        elif style == "upper":
            core = core.upper()
        elif style == "title":
            core = _first_upper(core)
        elif first and any(_cased(c) for c in token):
            lead, core, trail = "", _first_upper(token), ""
            first = False
        out.append(lead + core + trail)
    return "".join(out)


def _source_text(path: str, source: str) -> str:
    if source == "path":
        return path
    p = PurePath(path)
    return p.name if source == "name" else p.stem


def match_filename(pattern: str, text: str) -> dict[str, str] | None:
    """Placeholder values when ``text`` matches ``pattern`` exactly (TAGSCRIPT-11)."""
    regex = ""
    pos = 0
    for m in PLACEHOLDER.finditer(pattern):
        regex += re.escape(pattern[pos : m.start()])
        regex += f"(?P<{m['braced'] or m['bare']}>.+?)"
        pos = m.end()
    regex += re.escape(pattern[pos:])
    found = re.fullmatch(regex, text, re.S)
    return None if found is None else dict(found.groupdict())


# -- idempotence -----------------------------------------------------------------


@dataclass(frozen=True)
class IdempotenceCheck:
    issues: list[IdempotenceIssue]
    error: ScriptError | None = None
    """The second pass failed (the first pass's result triggers an error)."""

    @property
    def ok(self) -> bool:
        return not self.issues and self.error is None


def check_idempotence(script: TagScript, first: ScriptResult, ev: Evaluator) -> IdempotenceCheck:
    """Run ``script`` again on ``first``'s result; report what the second pass
    changes and the first action that changes it (TAGSCRIPT-16)."""
    second = run_script(script, first.album, ev, start=first.working)
    if second.error is not None:
        return IdempotenceCheck([], second.error)
    issues: list[IdempotenceIssue] = []
    album_ref = _ref(first.album)
    album_second: Fields = {}
    for ref, fields in second.working.items():
        before = first.working[ref]
        for name in [*before, *(k for k in fields if k not in before)]:
            a, b = before.get(name), fields.get(name)
            if (name in before) == (name in fields) and same_value(a, b):
                continue
            if ref == album_ref:
                album_second[name] = b
            elif (
                name in album_second and name in first.shared and same_value(album_second[name], b)
            ):
                continue  # follows the album's change (as in diffs())
            hits = second.hits.get((ref, name), [])
            index = hits[0] if hits else None
            issues.append(
                IdempotenceIssue(
                    target=ref,
                    field=name,
                    action_index=index,
                    action=None if index is None else second.label(index),
                    first=a,
                    second=b,
                )
            )
    return IdempotenceCheck(issues)
