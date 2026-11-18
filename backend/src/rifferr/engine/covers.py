"""Cover reviews on the engine side (S-COVERS-SEARCH COVERS-SEARCH-14)."""

from __future__ import annotations

from typing import Protocol

from rifferr.core.covers import CoverReview


class CoverReviewStore(Protocol):
    """Persisted cover reviews. The app DB in the worker."""

    def add(self, review: CoverReview) -> None: ...

    def get(self, review_id: str) -> CoverReview | None: ...


class InMemoryCoverStore:
    def __init__(self) -> None:
        self.items: dict[str, CoverReview] = {}

    def add(self, review: CoverReview) -> None:
        if review.id in self.items:
            raise ValueError(f"cover review {review.id} exists")
        self.items[review.id] = review

    def get(self, review_id: str) -> CoverReview | None:
        return self.items.get(review_id)
