"""Tag-script models (S-TAGSCRIPT TAGSCRIPT-1, -2, -4).

A script is an ordered list of actions. Each action is a mapping with exactly
one action key plus optional ``when``, ``scope`` and ``name``. The published
schema is ``schemas/tagscript.schema.json``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Annotated, Any, Final, Literal, Self, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

from rifferr.core.changeset import FieldName

ActionScope = Literal["album", "item"]
Idempotence = Literal["warn", "fail", "off"]
CaseStyle = Literal["lower", "upper", "title", "sentence"]
RegexFlag = Literal["i", "m", "s"]

ACTION_KEYS: Final = (
    "set",
    "copy",
    "move",
    "replace",
    "split",
    "join",
    "case",
    "from_filename",
    "delete",
    "append_unique",
)
COMMON_KEYS: Final = ("when", "scope", "name")
#: ``$field`` or ``${field}`` in a ``from_filename`` pattern.
PLACEHOLDER: Final = re.compile(r"\$\{(?P<braced>[a-z_][a-z0-9_]*)\}|\$(?P<bare>[a-z_][a-z0-9_]*)")

_SPEC = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class CopySpec(BaseModel):
    model_config = _SPEC

    from_: FieldName = Field(alias="from")
    to: FieldName


class ReplaceSpec(BaseModel):
    """Literal (``find``) or regex (``regex``) replacement in string values."""

    model_config = _SPEC

    fields: Annotated[list[FieldName], Field(min_length=1)]
    find: Annotated[str, Field(min_length=1)] | None = None
    regex: str | None = Field(
        default=None, description="Python re pattern; \\1 or \\g<name> in 'with'"
    )
    with_: str = Field(alias="with")
    flags: list[RegexFlag] = Field(default_factory=list[RegexFlag])

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        if (self.find is None) == (self.regex is None):
            raise ValueError("give exactly one of 'find' (literal text) or 'regex'")
        if self.find is not None and self.flags:
            raise ValueError("'flags' only apply to 'regex'")
        if self.regex is not None:
            try:
                compiled = re.compile(self.regex, self.re_flags())
                compiled.sub(self.with_, "")  # bad group references in 'with'
            except re.error as exc:
                raise ValueError(f"invalid regex: {exc}") from None
        return self

    def re_flags(self) -> int:
        bits = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
        out = 0
        for f in self.flags:
            out |= bits[f]
        return out


class SplitSpec(BaseModel):
    model_config = _SPEC

    field: FieldName
    by: Annotated[str, Field(min_length=1)]
    into: FieldName | None = None
    strip: bool = True


class JoinSpec(BaseModel):
    model_config = _SPEC

    field: FieldName
    with_: str = Field(alias="with")
    into: FieldName | None = None


class CaseSpec(BaseModel):
    model_config = _SPEC

    fields: Annotated[list[FieldName], Field(min_length=1)]
    style: CaseStyle
    keep: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list[str],
        description="Words left in exactly this form (matched case-insensitively)",
    )


class FromFilenameSpec(BaseModel):
    model_config = _SPEC

    pattern: str = Field(description="Literal text and $field placeholders, e.g. '$track - $title'")
    source: Literal["stem", "name", "path"] = "stem"

    @field_validator("pattern")
    @classmethod
    def _placeholders(cls, value: str) -> str:
        names = placeholder_names(value)
        if not names:
            raise ValueError("the pattern needs at least one $field placeholder")
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"placeholder used twice: {', '.join(dupes)}")
        return value


class AppendSpec(BaseModel):
    model_config = _SPEC

    field: FieldName
    values: Annotated[list[str], Field(min_length=1)]


def placeholder_names(pattern: str) -> list[str]:
    return [m["braced"] or m["bare"] for m in PLACEHOLDER.finditer(pattern)]


class ActionBase(BaseModel):
    model_config = _SPEC

    when: str | None = Field(default=None, description="Backend query (a beets query)")
    scope: ActionScope | None = Field(default=None, description="Default: the script's scope")
    name: str | None = Field(default=None, description="Label for logs and preview")


SetValue = Annotated[bool, Field(strict=True)] | int | float | str | list[str]


class SetAction(ActionBase):
    set: Annotated[dict[FieldName, SetValue], Field(min_length=1)]


class CopyAction(ActionBase):
    copy_: CopySpec = Field(alias="copy")


class MoveAction(ActionBase):
    move: CopySpec


class ReplaceAction(ActionBase):
    replace: ReplaceSpec


class SplitAction(ActionBase):
    split: SplitSpec


class JoinAction(ActionBase):
    join: JoinSpec


class CaseAction(ActionBase):
    case: CaseSpec


class FromFilenameAction(ActionBase):
    from_filename: FromFilenameSpec

    @model_validator(mode="after")
    def _item_only(self) -> Self:
        if self.scope == "album":
            raise ValueError("from_filename is item-scoped only")
        return self


class DeleteAction(ActionBase):
    delete: Annotated[list[FieldName], Field(min_length=1)]


class AppendUniqueAction(ActionBase):
    append_unique: AppendSpec


def action_key(value: Any) -> str | None:
    """The action key of a raw action mapping or a parsed action."""
    if isinstance(value, Mapping):
        keys = [k for k in cast(Mapping[str, Any], value) if k in ACTION_KEYS]
        return keys[0] if len(keys) == 1 else None
    if isinstance(value, BaseModel):
        fields = type(value).model_fields
        for key in ACTION_KEYS:
            if any((f.alias or name) == key for name, f in fields.items()):
                return key
    return None


def _tag(value: Any) -> str | None:
    key = action_key(value)
    return None if key is None else f"{key} action"


def _one_action_key(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value  # the union reports the type error
    mapping = cast(Mapping[str, Any], value)
    keys = [k for k in mapping if k in ACTION_KEYS]
    if len(keys) > 1:
        raise ValueError(f"an action takes exactly one action key, got {', '.join(keys)}")
    if not keys:
        others = [k for k in mapping if k not in COMMON_KEYS]
        hint = f" (unknown key: {', '.join(others)})" if others else ""
        raise ValueError(f"an action needs one of {', '.join(ACTION_KEYS)}{hint}")
    return mapping


Action = Annotated[
    Annotated[SetAction, Tag("set action")]
    | Annotated[CopyAction, Tag("copy action")]
    | Annotated[MoveAction, Tag("move action")]
    | Annotated[ReplaceAction, Tag("replace action")]
    | Annotated[SplitAction, Tag("split action")]
    | Annotated[JoinAction, Tag("join action")]
    | Annotated[CaseAction, Tag("case action")]
    | Annotated[FromFilenameAction, Tag("from_filename action")]
    | Annotated[DeleteAction, Tag("delete action")]
    | Annotated[AppendUniqueAction, Tag("append_unique action")],
    Discriminator(
        _tag,
        custom_error_type="action_type",
        custom_error_message="an action must be a mapping with one action key",
    ),
    BeforeValidator(_one_action_key),
]


def effective_scope(action: ActionBase, script_scope: ActionScope) -> ActionScope:
    if isinstance(action, FromFilenameAction):
        return "item"
    return action.scope or script_scope


def action_label(action: ActionBase, index: int) -> str:
    """``name``, or ``action N`` (1-based) for unnamed actions."""
    return action.name or f"action {index + 1}"


def _scope_problems(actions: list[Any], scope: ActionScope) -> list[str]:
    if scope != "item":
        return []
    return [
        f"actions[{i}]: album-scoped action in an item-scoped script"
        for i, a in enumerate(actions)
        if isinstance(a, ActionBase) and a.scope == "album"
    ]


class TagScript(BaseModel):
    """A script file, or a step's inline actions with its parameters (TAGSCRIPT-1)."""

    model_config = ConfigDict(frozen=True, extra="forbid", title="TagScript")

    scope: ActionScope = Field(default="album", description="Default scope of the actions")
    idempotence: Idempotence = Field(
        default="warn", description="Run the script twice and report a second-pass change"
    )
    actions: Annotated[list[Action], Field(min_length=1)]

    @model_validator(mode="after")
    def _scopes(self) -> Self:
        problems = _scope_problems(self.actions, self.scope)
        if problems:
            raise ValueError("; ".join(problems))
        return self


class TagScriptParams(BaseModel):
    """``with:`` of a ``tags.script`` step. Step parameters override the file's."""

    model_config = ConfigDict(extra="forbid")

    actions: Annotated[list[Action], Field(min_length=1)] | None = None
    script: str | None = Field(
        default=None, description="Script file, relative to the profile's scripts/ folder"
    )
    scope: ActionScope | None = None
    idempotence: Idempotence | None = None

    @field_validator("script")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        problem = script_path_problem(value)
        if problem:
            raise ValueError(problem)
        return value

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.actions is None) == (self.script is None):
            raise ValueError("give exactly one of 'actions' (inline) or 'script' (a file)")
        if self.actions is not None:
            problems = _scope_problems(self.actions, self.scope or "album")
            if problems:
                raise ValueError("; ".join(problems))
        return self

    def inline(self) -> TagScript | None:
        if self.actions is None:
            return None
        return TagScript(
            scope=self.scope or "album",
            idempotence=self.idempotence or "warn",
            actions=self.actions,
        )

    def override(self, script: TagScript) -> TagScript:
        """The file's script with this step's ``scope`` / ``idempotence``."""
        return with_overrides(script, scope=self.scope, idempotence=self.idempotence)


def with_overrides(
    script: TagScript,
    *,
    scope: ActionScope | None = None,
    idempotence: Idempotence | None = None,
) -> TagScript:
    """``script`` with another scope or idempotence; ``ValueError`` if the actions
    don't fit the new scope (TAGSCRIPT-4)."""
    update: dict[str, Any] = {}
    if scope is not None:
        update["scope"] = scope
    if idempotence is not None:
        update["idempotence"] = idempotence
    merged = script.model_copy(update=update)
    problems = _scope_problems(merged.actions, merged.scope)
    if problems:
        raise ValueError("; ".join(problems))
    return merged


def script_path_problem(path: str) -> str | None:
    """Why ``path`` can't name a file in ``scripts/`` (lexically), or None."""
    if "{{" in path:
        return None  # checked again after interpolation, at step start
    if not path.strip():
        return "must not be empty"
    p = PurePosixPath(path)
    if p.is_absolute() or path.startswith("~") or re.match(r"^[A-Za-z]:[\\/]", path):
        return "must be relative to the profile's scripts/ folder"
    if ".." in p.parts or "\\" in path:
        return "must stay inside the profile's scripts/ folder (no '..')"
    return None
