"""Round-trip YAML documents with source locations (WORKFLOW-4, WORKFLOW-11).

This is the only module in ``rifferr.core`` that touches ruamel.yaml. It parses
text (no file access) and maps data paths back to 1-based line/column.
"""

from __future__ import annotations

import io
from collections.abc import Mapping, Sequence
from typing import Any, Literal, cast

from ruamel.yaml import YAML
from ruamel.yaml import util as _ruamel_util
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.error import MarkedYAMLError, YAMLError
from ruamel.yaml.scalarbool import ScalarBoolean

from rifferr.core.workflow.errors import Path, ValidationError, format_path

Position = tuple[int, int]
LocateMode = Literal["key", "value", "nearest"]

# ruamel.yaml's type information is partial. Everything that crosses into it goes
# through these Any-typed handles so the rest of the module stays strictly typed.
_guess_indent: Any = cast(Any, _ruamel_util).load_yaml_guess_indent


def _lc(node: Any) -> Any:
    return node.lc


def _pos(raw: Any) -> Position:
    line, col = raw
    return (int(line) + 1, int(col) + 1)


def _yaml(mapping_indent: int = 2, sequence_indent: int = 2, offset: int = 0) -> YAML:
    yaml: Any = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=mapping_indent, sequence=sequence_indent, offset=offset)
    return yaml


class RoundTripDoc:
    """A parsed YAML document that can be edited and written back.

    ``dump()`` returns the original text unchanged until ``set()`` is called;
    after an edit, ruamel.yaml re-emits the document, keeping comments, key
    order and quoting.
    """

    def __init__(self, text: str, source: str, data: Any, yaml: YAML) -> None:
        self.text = text
        self.source = source
        self.data: Any = data
        self._yaml = yaml
        self._dirty = False

    @classmethod
    def detached(cls, source: str = "<workflow>") -> RoundTripDoc:
        """An empty document, for validating models that didn't come from a file.
        Errors from it carry no line/column."""
        return cls("", source, {}, _yaml())

    def plain(self) -> Any:
        """The document as plain dicts, lists and scalars."""
        return to_plain(self.data)

    def set(self, path: Path, value: Any) -> None:
        """Set ``value`` at ``path``; the parent must exist."""
        if not path:
            raise KeyError("empty path")
        parent: Any = self.data
        for part in path[:-1]:
            parent = _child(parent, part)
            if parent is _MISSING:
                raise KeyError(path)
        last = path[-1]
        is_map_key = isinstance(parent, CommentedMap) and isinstance(last, str)
        is_seq_index = isinstance(parent, CommentedSeq) and isinstance(last, int)
        if not (is_map_key or is_seq_index):
            raise KeyError(path)
        parent[last] = value
        self._dirty = True

    def dump(self) -> str:
        if not self._dirty:
            return self.text
        out = io.StringIO()
        yaml: Any = self._yaml
        yaml.dump(self.data, out)
        return out.getvalue()

    def locate(self, path: Path, mode: LocateMode = "value") -> Position | None:
        """1-based (line, column) for ``path``.

        - ``key``: the mapping key of the last part (unknown-key errors).
        - ``value``: the value at ``path`` (wrong values).
        - ``nearest``: the deepest existing ancestor (missing fields). A
          container that is a mapping value is reported at its key.
        """
        node: Any = self.data
        pos = _node_pos(node)
        for i, part in enumerate(path):
            child = _child(node, part)
            if child is _MISSING:
                return pos if mode == "nearest" else None
            last = i == len(path) - 1
            lc = _lc(node)
            if isinstance(node, CommentedMap):
                want_key = (last and mode == "key") or (
                    mode == "nearest" and isinstance(child, CommentedMap | CommentedSeq)
                )
                pos = _pos(lc.key(part) if want_key else lc.value(part))
            else:
                pos = _pos(lc.item(part))
            node = child
        return pos

    def error(self, path: Path, message: str, mode: LocateMode = "value") -> ValidationError:
        pos = self.locate(path, mode) or self.locate(path, "nearest")
        line, col = pos if pos else (None, None)
        return ValidationError(self.source, format_path(path), message, line, col)


class _Missing:
    pass


_MISSING: Any = _Missing()


def _child(node: Any, part: str | int) -> Any:
    if isinstance(node, Mapping) and isinstance(part, str):
        mapping = cast(Mapping[str, Any], node)
        return mapping.get(part, _MISSING)
    if isinstance(node, Sequence) and not isinstance(node, str) and isinstance(part, int):
        seq = cast(Sequence[Any], node)
        return seq[part] if 0 <= part < len(seq) else _MISSING
    return _MISSING


def _node_pos(node: Any) -> Position | None:
    if isinstance(node, CommentedMap | CommentedSeq):
        lc = _lc(node)
        return _pos((lc.line, lc.col))
    return None


def parse(text: str, source: str) -> RoundTripDoc:
    """Parse ``text``; raise ``YamlSyntaxError`` on bad YAML."""
    try:
        guessed: tuple[Any, int | None, int | None] = _guess_indent(text)
        _, indent, block_seq_indent = guessed
        yaml = _yaml(2, indent or 2, block_seq_indent or 0)
        loader: Any = yaml
        data: Any = loader.load(text)
    except YAMLError as exc:
        raise YamlSyntaxError(_syntax_error(exc, source)) from exc
    return RoundTripDoc(text, source, data, yaml)


class YamlSyntaxError(Exception):
    def __init__(self, error: ValidationError) -> None:
        self.error = error
        super().__init__(str(error))


def _syntax_error(exc: YAMLError, source: str) -> ValidationError:
    if isinstance(exc, MarkedYAMLError):
        marked: Any = exc
        mark: Any = marked.problem_mark or marked.context_mark
        problem = str(marked.problem or marked.context or "invalid YAML")
        if mark is not None:
            line, col = _pos((mark.line, mark.column))
            return ValidationError(source, "", problem, line, col)
        return ValidationError(source, "", problem)
    return ValidationError(source, "", str(exc))


def to_plain(value: Any) -> Any:
    """Convert ruamel.yaml round-trip types to plain Python values."""
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {str(k): to_plain(v) for k, v in mapping.items()}
    if isinstance(value, list):
        return [to_plain(v) for v in cast(list[Any], value)]
    if isinstance(value, bool | ScalarBoolean):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value
