"""Choice reviews (S-TAGS-CHOOSE): an album waiting for a person to pick one of several
outcomes, or to type values.

A review's content is fixed when it is created, so a decision always refers to what the
reviewer saw. Each option keeps its tag-script actions (as data) so the decision job can
run them again on the album's current values (TAGS-CHOOSE-5).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

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
    description: str | None = Field(
        default=None, description="The rule's own description (AI-SUGGEST-3)"
    )
    from_ai: bool = Field(
        default=False, description="Added because an AI suggestion named this rule (AI-SUGGEST-5)"
    )
    remembers: str | None = Field(
        default=None,
        description="Choosing this option also adds this name to the artist-name rules "
        "(TAGS-CHOOSE-13)",
    )


class CustomField(BaseModel):
    """A field offered for free entry, with its current value (TAGS-CHOOSE-4)."""

    model_config = _FROZEN

    field: str
    kind: CustomKind = "text"
    value: FieldValue = None
    suggestions: list[str] = Field(
        default_factory=list[str],
        description="Known values offered while typing; free entry still allowed "
        "(TAGS-CHOOSE-4: the options_from rules file's values for its target)",
    )


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
    ai_input: ChoiceSuggestInput | None = Field(
        default=None, description="What an AI suggestion request sends (AI-SUGGEST-2)"
    )
    ai: ChoiceAI | None = Field(default=None, description="AI suggestions (AI-SUGGEST-9)")

    def hold_key(self) -> str:
        return self.album_id

    def option(self, key: str) -> ChoiceOffer | None:
        return next((o for o in self.options if o.key == key), None)


# -- AI suggestions (F-AI-SUGGEST) ----------------------------------------------------

SuggestMode = Literal["off", "unsure", "always"]
#: The AI task that suggests options for a choice review (AI-SUGGEST-4).
SUGGEST_TASK = "choice.suggest"


class SuggestOption(BaseModel):
    """An option the review offers, as the AI sees it (AI-SUGGEST-2)."""

    model_config = _FROZEN

    key: str
    label: str
    description: str | None = None
    rule: bool = False
    fallback: bool = False
    matched: int = 0


class SuggestRule(BaseModel):
    """A rule of the rules file. ``actions`` are kept for choosing it, never sent."""

    model_config = _FROZEN

    label: str
    description: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])


class ChoiceSuggestInput(BaseModel):
    """The ``choice.suggest`` task's input, fixed when the review is created."""

    model_config = _FROZEN

    album: str
    message: str | None = None
    shown: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    rank_by: str | None = None
    rank_values: list[str] = Field(default_factory=list[str])
    options: list[SuggestOption] = Field(default_factory=list[SuggestOption])
    rules: list[SuggestRule] = Field(default_factory=list[SuggestRule])
    custom: list[str] = Field(default_factory=list[str])
    context: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    guidance: str | None = None
    provider: str | None = None
    max_suggestions: int = Field(default=3, ge=1, le=10)
    allow_new: bool = True


class ChoiceAISuggestion(BaseModel):
    """One ranked AI suggestion (AI-SUGGEST-4, -5). Exactly one of ``option`` (an offered
    option's key, or a rule the AI named) and ``values`` (a new custom value) is set."""

    model_config = _FROZEN

    rank: int
    label: str
    option: str | None = None
    rule: str | None = Field(default=None, description="A rules-file label not offered")
    values: dict[str, list[str]] = Field(default_factory=dict[str, list[str]])
    new: bool = False
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""


class ChoiceAI(BaseModel):
    """AI suggestions stored with a review (AI-SUGGEST-9)."""

    model_config = _FROZEN

    suggestions: list[ChoiceAISuggestion] = Field(default_factory=list[ChoiceAISuggestion])
    provider: str | None = None
    model: str | None = None
    prompt_version: int | None = None
    usage: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    cached: bool = False
    auto: bool = Field(default=False, description="Asked when the review opened (AI-SUGGEST-8)")
    at: datetime | None = None
    error: str | None = None


def rules_unsure(options: list[SuggestOption]) -> bool:
    """Whether ``unsure`` mode asks the AI (AI-SUGGEST-7): no option, only fallback
    options, or a tie between the top two ranked rule options."""
    if not options:
        return True
    if all(o.fallback for o in options):
        return True
    ranked = [o for o in options if o.rule]
    return len(ranked) >= 2 and ranked[0].matched == ranked[1].matched


def with_ai(review: ChoiceReview, ai: ChoiceAI) -> ChoiceReview:
    """``review`` with ``ai`` stored; a suggested rule that isn't offered becomes an extra
    option (``a<n>``, ``from_ai``) so choosing it is an ordinary decision (AI-SUGGEST-6)."""
    options = [o for o in review.options if not o.from_ai]
    rules = {r.label: r for r in (review.ai_input.rules if review.ai_input else [])}
    suggestions: list[ChoiceAISuggestion] = []
    for s in ai.suggestions:
        if s.rule is not None and s.rule in rules:
            rule = rules[s.rule]
            key = f"a{len([o for o in options if o.from_ai])}"
            options.append(
                ChoiceOffer(
                    key=key,
                    label=rule.label,
                    actions=rule.actions,
                    rule=True,
                    description=rule.description,
                    from_ai=True,
                )
            )
            s = s.model_copy(update={"option": key})
        suggestions.append(s)
    stored = ai.model_copy(update={"suggestions": suggestions})
    return review.model_copy(update={"options": options, "ai": stored})


ChoiceReview.model_rebuild()
