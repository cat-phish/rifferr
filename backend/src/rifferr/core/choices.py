"""Choice reviews (S-TAGS-CHOOSE): an album waiting for a person to pick one of several
outcomes, or to type values.

A review's content is fixed when it is created, so a decision always refers to what the
reviewer saw. Each option keeps its tag-script actions (as data) so the decision job can
run them again on the album's current values (TAGS-CHOOSE-5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from rifferr.core.changeset import TargetRef
from rifferr.core.snapshot import FieldValue

ChoiceState = Literal["pending", "chosen", "skipped", "rejected", "cancelled"]
ChoiceDecision = Literal["choose", "skip", "reject"]
ChoiceOnReject = Literal["stop", "continue"]
CustomKind = Literal["text", "list"]

#: The option key of free entry (TAGS-CHOOSE-1).
CUSTOM = "custom"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ChoicePreview(BaseModel):
    """One field change an option makes, as the reviewer sees it (TAGS-CHOOSE-4)."""

    model_config = _FROZEN

    target: TargetRef
    field: str
    before: FieldValue
    after: FieldValue
    deleted: bool = False


class ChoiceOffer(BaseModel):
    """One option offered for an album (TAGS-CHOOSE-1)."""

    model_config = _FROZEN

    key: str = Field(description="Stable id of the option within the review")
    label: str
    actions: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="Tag-script actions, run again on the album's current values when chosen",
    )
    preview: list[ChoicePreview] = Field(default_factory=list[ChoicePreview])
    rule: bool = Field(default=False, description="From the rules file (TAGS-CHOOSE-8)")
    matched: int = Field(default=0, description="How many rank_by values the rule matches")


class CustomField(BaseModel):
    """A field offered for free entry, with its current value (TAGS-CHOOSE-4)."""

    model_config = _FROZEN

    field: str
    kind: CustomKind = "text"
    value: FieldValue = None


class LearnSettings(BaseModel):
    """Where a custom decision is remembered (TAGS-CHOOSE-10)."""

    model_config = _FROZEN

    rules_file: str = Field(description="Relative to the profile's rules/ folder")
    rank_by: str
    target: str
    unmatched: list[str] = Field(
        default_factory=list[str], description="The album's rank_by values no rule matched"
    )


class ChoiceReview(BaseModel):
    """An album waiting in the review queue for a choice (TAGS-CHOOSE-3)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    profile: str
    run_id: str
    step_id: str
    album_id: str
    label: str
    message: str | None = None
    shown: dict[str, FieldValue] = Field(default_factory=dict[str, FieldValue])
    options: list[ChoiceOffer]
    suggested: str | None = Field(default=None, description="Key of the suggested option")
    custom: list[CustomField] = Field(default_factory=list[CustomField])
    known_name: str | None = Field(
        default=None, description="Field whose value 'This is one artist' remembers"
    )
    known_name_value: str | None = None
    known_name_option: str | None = Field(
        default=None, description="Label 'This is one artist' decides with (TAGS-CHOOSE-13)"
    )
    known_name_actions: list[dict[str, Any]] = Field(
        default_factory=list[dict[str, Any]],
        description="That option's actions; none leaves the album unchanged",
    )
    learn: LearnSettings | None = None
    on_reject: ChoiceOnReject = "stop"
    state: ChoiceState = "pending"
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    chosen: str | None = Field(default=None, description="Key of the chosen option")
    note: str | None = None
    changeset_id: str | None = None
    learned: str | None = Field(
        default=None, description="What the decision added to a rules file (TAGS-CHOOSE-10, -13)"
    )

    def hold_key(self) -> str:
        return self.album_id

    def option(self, key: str) -> ChoiceOffer | None:
        return next((o for o in self.options if o.key == key), None)
