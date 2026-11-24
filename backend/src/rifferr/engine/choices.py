"""Choice reviews on the engine side (S-TAGS-CHOOSE TAGS-CHOOSE-3)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol

from rifferr.core.choices import CUSTOM, ChoiceReview
from rifferr.core.snapshot import FieldValue


class ChoiceReviewStore(Protocol):
    """Persisted choice reviews. The app DB in the worker."""

    def add(self, review: ChoiceReview) -> None: ...

    def get(self, review_id: str) -> ChoiceReview | None: ...


class InMemoryChoiceStore:
    def __init__(self) -> None:
        self.items: dict[str, ChoiceReview] = {}

    def add(self, review: ChoiceReview) -> None:
        if review.id in self.items:
            raise ValueError(f"choice review {review.id} exists")
        self.items[review.id] = review

    def get(self, review_id: str) -> ChoiceReview | None:
        return self.items.get(review_id)


class ChoiceDecisionError(ValueError):
    """The decision doesn't fit the review (an unknown option or field)."""


def decision_actions(
    review: ChoiceReview,
    *,
    option: str | None,
    values: Mapping[str, FieldValue] | None,
    known_name: bool,
    escape: Callable[[str], str],
) -> tuple[str, list[dict[str, Any]]]:
    """The label and tag-script actions a decision applies (TAGS-CHOOSE-5, -13).

    ``custom`` sets each given field (an empty value deletes it); values are made literal
    with ``escape``, since ``set`` values are templates.
    """
    if known_name:
        if review.known_name is None or review.known_name_option is None:
            raise ChoiceDecisionError("this review doesn't offer 'This is one artist'")
        return review.known_name_option, list(review.known_name_actions)
    if option == CUSTOM:
        allowed = {c.field for c in review.custom}
        if not allowed:
            raise ChoiceDecisionError("this review has no custom fields")
        given = dict(values or {})
        unknown = sorted(set(given) - allowed)
        if unknown:
            raise ChoiceDecisionError(f"not a custom field of this review: {', '.join(unknown)}")
        if not given:
            raise ChoiceDecisionError("custom needs values")
        actions: list[dict[str, Any]] = []
        for name, value in given.items():
            if value is None or value == "" or value == []:
                actions.append({"delete": [name]})
            elif isinstance(value, list):
                actions.append({"set": {name: [escape(str(v)) for v in value if str(v).strip()]}})
            elif isinstance(value, str):
                actions.append({"set": {name: escape(value)}})
            else:
                actions.append({"set": {name: value}})
        return "custom", actions
    if option is None:
        raise ChoiceDecisionError("choose needs an option")
    offer = review.option(option)
    if offer is None:
        raise ChoiceDecisionError(f"no option {option!r} in this review")
    return offer.label, list(offer.actions)
