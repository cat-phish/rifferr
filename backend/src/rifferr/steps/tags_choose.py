"""The ``tags.choose`` step (S-TAGS-CHOOSE): a review with choices.

For each album, every option's tag-script actions run on the album's values
(``rifferr.tagscript``); the options that change something are offered in the review
queue, the likely one preselected, with free entry for the ``custom`` fields. Only the
album's lane waits. The decision becomes one ChangeSet applied through the engine (the
worker's ``choice_apply`` job). Options are data: no code runs, so no trust is needed.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rifferr.backends.base import BackendCapabilities, LibraryBackend, QueryError, TemplateError
from rifferr.choice_rules import (
    RulesError,
    RulesFile,
    load_rules,
    resolve_rules,
    rule_values,
    substitute,
)
from rifferr.core.changeset import (
    ChangeSetProposal,
    FieldName,
)
from rifferr.core.choices import (
    CUSTOM,
    ChoiceOffer,
    ChoicePreview,
    CustomField,
    CustomKind,
    LearnSettings,
)
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, label
from rifferr.core.workflow.template_fields import TEMPLATE_EXTRA
from rifferr.engine import StepContext, StepFailure, StepResult
from rifferr.engine.steps import ChoiceRequest
from rifferr.steps.tagscript import proposal
from rifferr.tagscript.evaluator import ScriptResult, run_script
from rifferr.tagscript.models import Action, TagScript, script_path_problem

ACTIONS_ADAPTER_KEY = "actions"


class Suggest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    when: str = Field(
        min_length=1, description="Backend query (a beets query); may use album facts"
    )


class ChoiceOption(BaseModel):
    """One outcome a person can choose (TAGS-CHOOSE-1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    suggest: Suggest | None = Field(
        default=None, description="Preselect this option when its query matches (TAGS-CHOOSE-2)"
    )
    actions: list[Action] = Field(
        description="Tag-script actions; empty only for the known_name_option (TAGS-CHOOSE-13)"
    )


class TagsChooseParams(BaseModel):
    """``with:`` of a ``tags.choose`` step."""

    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(
        default=None,
        description="Albums to ask about: a backend query (a beets query) that may use album "
        "facts (C-ALBUM-FACTS). Default: every album of the step",
    )
    message: str | None = Field(
        default=None,
        description="Shown for each album: a beets template",
        json_schema_extra=TEMPLATE_EXTRA,
    )
    show: list[FieldName] = Field(default_factory=list[str], description="Fields to show")
    options: list[ChoiceOption] = Field(default_factory=list[ChoiceOption])
    options_from: str | None = Field(
        default=None, description="Rules file with more options, relative to the profile's rules/"
    )
    rank_by: FieldName | None = Field(
        default=None,
        description="List field: rules are ranked by how many of its values they match",
    )
    custom: list[FieldName] = Field(
        default_factory=list[str], description="Fields offered for free entry"
    )
    when_single: Literal["review", "auto"] = Field(
        default="review",
        description="auto: apply the only option that changes something without review "
        "(not with custom fields)",
    )
    on_reject: Literal["stop", "continue"] = Field(
        default="stop", description="On reject, stop the album's lane or let it go on"
    )
    learn: bool = Field(
        default=False,
        description="Add a rule to the rules file for a custom decision (TAGS-CHOOSE-10)",
    )
    known_name: FieldName | None = Field(
        default=None,
        description="Remember this field's value in the artist-name rules when the "
        "known_name_option is chosen (TAGS-CHOOSE-13)",
    )
    known_name_option: str | None = Field(
        default=None,
        description="The option that remembers the name; always offered while the field has "
        "a value, even when it changes nothing (its actions may be empty)",
    )

    @field_validator("options_from")
    @classmethod
    def _relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        problem = script_path_problem(value)
        if problem:
            raise ValueError(problem.replace("scripts/", "rules/"))
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if not self.options and self.options_from is None and not self.custom:
            raise ValueError("give options, options_from or custom fields")
        if self.rank_by is not None and self.options_from is None:
            raise ValueError("rank_by ranks the rules of options_from")
        if self.learn and (self.options_from is None or self.rank_by is None or not self.custom):
            raise ValueError("learn needs options_from, rank_by and custom fields")
        labels = [o.label for o in self.options]
        if len(set(labels)) != len(labels):
            raise ValueError("option labels must be unique")
        if self.known_name_option is not None and self.known_name is None:
            raise ValueError("known_name_option needs known_name")
        empty = [o.label for o in self.options if not o.actions]
        if any(label != self.known_name_option for label in empty):
            raise ValueError("only the known_name_option may have no actions")
        return self


def known_name_value(params: TagsChooseParams, album: AlbumSnapshot) -> str | None:
    """The name the known_name_option remembers (TAGS-CHOOSE-13), if the field has one."""
    if params.known_name is None:
        return None
    raw = album.fields.get(params.known_name)
    return str(raw) if raw not in (None, "", []) and not isinstance(raw, list) else None


def with_facts(backend: LibraryBackend, album: AlbumSnapshot, text: str) -> AlbumSnapshot:
    """``album`` with its computed facts when ``text`` names one (ALBUM-FACTS-3)."""
    if "rf_" not in text:
        return album
    facts = backend.album_facts(album)
    return album.model_copy(update={"fields": {**album.fields, **facts}})


def _matches(backend: LibraryBackend, query: str, album: AlbumSnapshot) -> bool:
    return backend.matches(query, with_facts(backend, album, query))


def _run(actions: list[Any], album: AlbumSnapshot, backend: LibraryBackend) -> ScriptResult:
    script = TagScript.model_validate({"idempotence": "off", ACTIONS_ADAPTER_KEY: actions})
    return run_script(script, album, backend)


def _preview(result: ScriptResult) -> list[ChoicePreview]:
    return [
        ChoicePreview(
            target=d.target, field=d.field, before=d.before, after=d.after, deleted=d.deleted
        )
        for d in result.diffs()
    ]


def _dump(actions: list[Any]) -> list[dict[str, Any]]:
    return [
        a.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(a, BaseModel)
        else dict(a)
        for a in actions
    ]


def rank_values(album: AlbumSnapshot, field: str) -> list[str]:
    value = album.fields.get(field)
    if isinstance(value, list):
        return [v for v in value if v]
    if value is None or value == "":
        return []
    return [str(value)]


def _custom_kind(backend: LibraryBackend, name: str, value: FieldValue) -> CustomKind:
    if isinstance(value, list):
        return "list"
    try:
        return "list" if isinstance(backend.coerce(name, "a"), list) else "text"
    except (ValueError, TypeError):
        return "text"


class _Offers:
    """Options offered for one album, in order, with their rank."""

    def __init__(self) -> None:
        self.offers: list[ChoiceOffer] = []
        self.labels: set[str] = set()

    def add(self, offer: ChoiceOffer) -> None:
        self.offers.append(offer)
        self.labels.add(offer.label)


def rule_offers(
    rules: RulesFile,
    album: AlbumSnapshot,
    backend: LibraryBackend,
    rank_by: str | None,
    taken: set[str],
) -> tuple[list[ChoiceOffer], list[str]]:
    """Rules that match the album, ranked by how many ``rank_by`` values each matches
    (ties keep file order), then fallback options for values no rule matched
    (TAGS-CHOOSE-8, -9). Also returns those unmatched values."""
    values = rank_values(album, rank_by) if rank_by is not None else []
    matched: set[str] = set()
    ranked: list[tuple[int, int, ChoiceOffer]] = []
    for index, rule in enumerate(rules.rules):
        if rule.when is None:  # a known value only (TAGS-CHOOSE-8)
            continue
        if rank_by is None:
            count = 1 if _matches(backend, rule.when, album) else 0
        else:
            hits = [
                v
                for v in values
                if _matches(
                    backend,
                    rule.when,
                    album.model_copy(update={"fields": {**album.fields, rank_by: [v]}}),
                )
            ]
            matched.update(hits)
            count = len(hits)
        if count == 0 or rule.label in taken:
            continue
        result = _run(list(rule.actions), album, backend)
        if result.error is not None or not result.diffs():
            continue
        offer = ChoiceOffer(
            key=f"r{index}",
            label=rule.label,
            actions=_dump(list(rule.actions)),
            preview=_preview(result),
            rule=True,
            matched=count,
        )
        ranked.append((count, len(ranked), offer))
    unmatched = [v for v in values if v not in matched]
    if rules.fallback is not None and unmatched:
        by_label: dict[str, int] = {}
        for value in unmatched:
            template = substitute(rules.fallback.template, "value", backend.escape_template(value))
            try:
                choice = backend.evaluate(template, album).strip()
            except TemplateError:
                choice = value
            if choice:
                by_label[choice] = by_label.get(choice, 0) + 1
        existing = taken | {o.label for _, _, o in ranked}
        for index, (choice, count) in enumerate(by_label.items()):
            if choice in existing:
                continue
            actions = substitute(rules.fallback.actions, "choice", backend.escape_template(choice))
            try:
                result = _run(actions, album, backend)
            except ValueError as exc:
                raise StepFailure(f"rules file fallback actions: {exc}") from None
            if result.error is not None or not result.diffs():
                continue
            offer = ChoiceOffer(
                key=f"f{index}",
                label=choice,
                actions=_dump(actions),
                preview=_preview(result),
                rule=True,
                matched=count,
            )
            ranked.append((count, len(ranked), offer))
    ranked.sort(key=lambda r: (-r[0], r[1]))
    return [o for _, _, o in ranked], unmatched


class TagsChooseStep:
    name = "tags.choose"
    waits = "always"  # may put items in the review queue (UI-REFRESH-13)
    params_model = TagsChooseParams
    default_apply = "auto"  # the choice was the review; when_single: auto (TAGS-CHOOSE-7)
    requires = BackendCapabilities(query=True, templates=True, apply=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, TagsChooseParams)
        backend = ctx.backend
        rules: RulesFile | None = None
        if params.options_from is not None:
            if ctx.profile_dir is None:
                raise StepFailure("this run has no profile folder to read rules from")
            try:
                rules = load_rules(resolve_rules(ctx.profile_dir, params.options_from))
            except RulesError as exc:
                raise StepFailure(str(exc)) from None
            if params.learn and rules.target is None:
                raise StepFailure(f"rules file {params.options_from}: learn needs a target")
        asked = applied = skipped = 0
        choices: list[dict[str, Any]] = []
        for album in ctx.albums():
            if ctx.cancelled:
                break
            try:
                if params.query is not None and not _matches(backend, params.query, album):
                    continue
                outcome = self._album(ctx, params, rules, album)
            except (QueryError, TemplateError) as exc:
                ctx.fail_target(album.id, str(exc))
                continue
            if outcome is None:
                skipped += 1
                continue
            choices.append(outcome)
            if outcome["decided_by"] == "auto":
                applied += 1
            else:
                asked += 1
        if asked:
            ctx.log(f"waiting for {asked} choice(s)")
        return StepResult(
            outputs={
                "asked": asked,
                "applied": applied,
                "unchanged": skipped,
                "choices": choices,  # pyright: ignore[reportArgumentType]
                "changesets": len(ctx.proposed),
            }
        )

    def _album(
        self,
        ctx: StepContext,
        params: TagsChooseParams,
        rules: RulesFile | None,
        album: AlbumSnapshot,
    ) -> dict[str, Any] | None:
        backend = ctx.backend
        offers = _Offers()
        suggested: str | None = None
        known_actions: list[dict[str, Any]] = []
        for index, option in enumerate(params.options):  # TAGS-CHOOSE-1
            changes = False
            preview: list[ChoicePreview] = []
            if option.actions:  # empty only for the known_name_option
                result = _run(list(option.actions), album, backend)
                if result.error is not None:
                    ctx.log(
                        f"{album.id}: option {option.label!r}: {result.error}", "warning", album.id
                    )
                    continue
                changes = bool(result.diffs())
                preview = _preview(result)
            key = f"o{index}"
            remembers: str | None = None
            if option.label == params.known_name_option:
                known_actions = _dump(list(option.actions))
                remembers = known_name_value(params, album)
            if not changes and remembers is None:
                continue
            offers.add(
                ChoiceOffer(
                    key=key,
                    label=option.label,
                    actions=_dump(list(option.actions)),
                    preview=preview,
                    remembers=remembers,
                )
            )
            if (
                suggested is None
                and option.suggest is not None
                and _matches(backend, option.suggest.when, album)
            ):  # TAGS-CHOOSE-2
                suggested = key
        unmatched: list[str] = []
        if rules is not None:
            ranked, unmatched = rule_offers(rules, album, backend, params.rank_by, offers.labels)
            for offer in ranked:
                offers.add(offer)
            if suggested is None and ranked:
                suggested = ranked[0].key
        album_label = label(album)
        if not offers.offers and not params.custom:
            ctx.log(f"{album.id}: no option changes anything", target=album.id)
            return None
        record: dict[str, Any] = {
            "album_id": album.id,
            "offered": [o.label for o in offers.offers],
            "suggested": next((o.label for o in offers.offers if o.key == suggested), None),
        }
        if (
            params.when_single == "auto"
            and len(offers.offers) == 1
            and not params.custom
            and offers.offers[0].remembers is None  # remembering a name is the person's call
        ):
            only = offers.offers[0]  # TAGS-CHOOSE-7
            result = _run(only.actions, album, backend)
            made = proposal(result, f"{ctx.step.id}")
            if made is not None:
                ctx.propose(
                    ChangeSetProposal(rationale=only.label, targets=list(made.targets or []))
                )
            ctx.log(f"{album.id}: {only.label} (the only option)", target=album.id)
            return {**record, "chosen": only.label, "decided_by": "auto"}
        message: str | None = None
        if params.message is not None:
            message = backend.evaluate(params.message, album)
        known = rule_values(rules) if rules is not None else []
        custom = [
            CustomField(
                field=f,
                kind=_custom_kind(backend, f, album.fields.get(f)),
                value=album.fields.get(f),
                suggestions=known if rules is not None and f == rules.target else [],
            )
            for f in params.custom
        ]
        known_value = known_name_value(params, album)
        learn: LearnSettings | None = None
        if params.learn and rules is not None and rules.target is not None:
            assert params.options_from is not None
            assert params.rank_by is not None
            learn = LearnSettings(
                rules_file=params.options_from,
                rank_by=params.rank_by,
                target=rules.target,
                unmatched=unmatched,
            )
        review_id = ctx.request_choice(
            ChoiceRequest(
                album_id=album.id,
                label=album_label,
                options=offers.offers,
                message=message,
                shown={f: album.fields.get(f) for f in params.show},
                suggested=suggested,
                custom=custom,
                known_name=params.known_name if known_value is not None else None,
                known_name_value=known_value,
                known_name_option=params.known_name_option or "One artist"
                if known_value is not None
                else None,
                known_name_actions=known_actions if known_value is not None else [],
                learn=learn,
                on_reject=params.on_reject,
            )
        )
        return {**record, "review_id": review_id, "chosen": None, "decided_by": None}


__all__ = ["CUSTOM", "TagsChooseParams", "TagsChooseStep"]
