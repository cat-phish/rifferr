"""ChangeSets (C-CHANGESET) and journal entries (C-JOURNAL).

Every tag change in Rifferr is a ChangeSet. Steps propose them through
``ctx.propose`` with a ``ChangeSetProposal``; only the engine applies them,
through ``LibraryBackend.apply``, after the journal has recorded the intent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from rifferr.core.selection import Scope
from rifferr.core.snapshot import FieldValue

FIELD_NAME: Final = re.compile(r"^[a-z_][a-z0-9_]*$")

FieldOp = Literal["set", "delete"]
ChangeSetState = Literal[
    "proposed", "applied", "partially_applied", "rejected", "conflicted", "undone"
]
#: States a ChangeSet can't leave except by undo (C-JOURNAL).
RESOLVED_STATES: Final = frozenset({"applied", "partially_applied", "rejected", "conflicted"})


def _field_name(name: str) -> str:
    if not FIELD_NAME.fullmatch(name):
        raise ValueError(f"field name {name!r} must match {FIELD_NAME.pattern}")
    return name


FieldName = Annotated[str, AfterValidator(_field_name)]

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class TargetRef(BaseModel):
    """One album or item, by the backend id from its snapshot."""

    model_config = _FROZEN

    scope: Scope
    id: str = Field(min_length=1)

    def label(self) -> str:
        return f"{self.scope} {self.id}"


class _FieldChangeBase(BaseModel):
    model_config = _FROZEN

    field: FieldName
    op: FieldOp = "set"
    new: FieldValue = Field(default=None, description="The new value; required for op 'set'.")
    rationale: str | None = None

    @model_validator(mode="after")
    def _check_op(self) -> Self:
        if self.op == "set" and self.new is None:
            raise ValueError("op 'set' needs a 'new' value (use op 'delete' to remove a field)")
        if self.op == "delete" and self.new is not None:
            raise ValueError("op 'delete' takes no 'new' value")
        return self


class FieldChange(_FieldChangeBase):
    """One field change. ``old`` is the value the proposer saw (``None`` = absent)."""

    old: FieldValue = None


class ProposedFieldChange(_FieldChangeBase):
    """A field change as a step proposes it. Without ``old`` the engine fills it in."""

    old: FieldValue = Field(
        default=None,
        description="The value the proposer saw. Omit it and the engine fills it in "
        "from the snapshot the step was given.",
    )

    def has_old(self) -> bool:
        return "old" in self.model_fields_set


class TargetChange(BaseModel):
    model_config = _FROZEN

    target: TargetRef
    changes: list[FieldChange] = Field(min_length=1)


class ProposedTargetChange(BaseModel):
    model_config = _FROZEN

    target: TargetRef
    changes: list[ProposedFieldChange] = Field(min_length=1)


class ChangeSource(BaseModel):
    model_config = _FROZEN

    kind: Literal["step", "manual", "chat", "ai"]
    name: str = Field(min_length=1, description="e.g. the step type, or 'ui'")


class ChangeSet(BaseModel):
    """Proposed field changes to one album and/or its items (CHANGESET-1, -14)."""

    model_config = _FROZEN

    id: str
    profile: str
    run_id: str | None = None
    step_id: str | None = None
    source: ChangeSource
    rationale: str | None = None
    created_at: datetime
    targets: list[TargetChange] = Field(min_length=1)

    def field_changes(self) -> list[tuple[TargetRef, FieldChange]]:
        """Every field change in order. Indexes into this list identify a field change
        in review decisions (CHANGESET-9)."""
        return [(t.target, c) for t in self.targets for c in t.changes]

    def subset(self, accepted: list[int]) -> ChangeSet:
        """The same ChangeSet with only the field changes at ``accepted`` indexes."""
        wanted = set(accepted)
        flat = self.field_changes()
        if not wanted <= set(range(len(flat))):
            raise ValueError(f"field change indexes out of range: {sorted(wanted)}")
        targets: list[TargetChange] = []
        index = 0
        for t in self.targets:
            kept = [c for i, c in enumerate(t.changes, start=index) if i in wanted]
            index += len(t.changes)
            if kept:
                targets.append(TargetChange(target=t.target, changes=kept))
        if not targets:
            raise ValueError("an accepted subset needs at least one field change")
        return self.model_copy(update={"targets": targets})


class ChangeSetProposal(BaseModel):
    """What a step proposes (CHANGESET-3). Published as ``changeset-proposal.schema.json``;
    also the stdout format of ``shell`` steps with ``emits: changes``.

    Exactly one of ``targets`` or the short form ``changes`` (which applies to the
    step's current album or item).
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        title="ChangeSetProposal",
        json_schema_extra={
            "oneOf": [
                {"required": ["targets"]},
                {"required": ["changes"]},
            ]
        },
    )

    rationale: str | None = None
    targets: list[ProposedTargetChange] | None = Field(default=None, min_length=1)
    changes: list[ProposedFieldChange] | None = Field(
        default=None,
        min_length=1,
        description="Short form: field changes to the step's current album or item.",
    )

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        if (self.targets is None) == (self.changes is None):
            raise ValueError("a proposal needs exactly one of 'targets' or 'changes'")
        return self


class ChangeProblem(BaseModel):
    """Why a backend refuses a field change (``LibraryBackend.check``, CHANGESET-2)."""

    model_config = _FROZEN

    target: TargetRef
    field: str
    message: str


class Conflict(BaseModel):
    """A field whose current value is not the one the change expected (CHANGESET-8)."""

    model_config = _FROZEN

    target: TargetRef
    field: str
    expected: FieldValue
    current: FieldValue


class FieldEffect(BaseModel):
    """What applying actually changed, including propagated item fields (CHANGESET-11)."""

    model_config = _FROZEN

    target: TargetRef
    field: str
    before: FieldValue
    after: FieldValue
    existed_before: bool = True
    """False: the field didn't exist before, so undo deletes it."""


class WriteFailure(BaseModel):
    """A file whose tags couldn't be written after the library change (CHANGESET-10)."""

    model_config = _FROZEN

    target: TargetRef
    path: str
    error: str


class ApplyResult(BaseModel):
    model_config = _FROZEN

    applied: bool
    conflicts: list[Conflict] = Field(default_factory=list[Conflict])
    effects: list[FieldEffect] = Field(default_factory=list[FieldEffect])
    write_failures: list[WriteFailure] = Field(default_factory=list[WriteFailure])


JournalState = Literal["pending", "applied", "failed"]


class JournalEntry(BaseModel):
    """Write-ahead record of one apply (C-JOURNAL JOURNAL-1, -2)."""

    model_config = _FROZEN

    id: str
    profile: str
    changeset_id: str
    run_id: str | None = None
    accepted: list[int] | None = None
    """Field-change indexes when only part of the ChangeSet was accepted."""
    state: JournalState = "pending"
    intended: list[TargetChange]
    effects: list[FieldEffect] = Field(default_factory=list[FieldEffect])
    write_failures: list[WriteFailure] = Field(default_factory=list[WriteFailure])
    conflicts: list[Conflict] = Field(default_factory=list[Conflict])
    error: str | None = None
    uncertain: bool = False
    undo_of: str | None = None
    undone_by: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


# -- helpers shared by backends (pure) ------------------------------------------

#: Reads a target's current fields, or None if the target doesn't exist.
CurrentFields = Callable[[TargetRef], Mapping[str, FieldValue] | None]


def find_conflicts(changes: ChangeSet, current: CurrentFields) -> list[Conflict]:
    """Field changes whose ``old`` differs from the current value (CHANGESET-8)."""
    conflicts: list[Conflict] = []
    for target, change in changes.field_changes():
        fields = current(target) or {}
        now = fields.get(change.field)
        if not same_value(now, change.old):
            conflicts.append(
                Conflict(target=target, field=change.field, expected=change.old, current=now)
            )
    return conflicts


def same_value(a: FieldValue, b: FieldValue) -> bool:
    """Equality that keeps ``True`` and ``1`` apart, as tag values must."""
    return type(a) is type(b) and a == b


def diff_effects(
    target: TargetRef,
    before: Mapping[str, FieldValue],
    after: Mapping[str, FieldValue],
    ignore: Iterable[str] = (),
) -> list[FieldEffect]:
    """Every field that differs between ``before`` and ``after`` (CHANGESET-11)."""
    skip = set(ignore)
    effects: list[FieldEffect] = []
    for field in sorted((set(before) | set(after)) - skip):
        old, new = before.get(field), after.get(field)
        existed = field in before
        if existed == (field in after) and same_value(old, new):
            continue
        effects.append(
            FieldEffect(target=target, field=field, before=old, after=new, existed_before=existed)
        )
    return effects
