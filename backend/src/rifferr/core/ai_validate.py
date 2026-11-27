"""Vocabulary matching and lookalike characters for AI answers (AI-CORE-13, -22).

Pure. A model writes ``Alt/Indie`` where the user's vocabulary has ``Alt／Indie``; the match
key makes them equal, and the result always takes the user's spelling. The lookalike map
is user data (non-negotiable 1): without one, only Unicode NFKC folds characters.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MatchMode = Literal["strict", "flag", "open"]
REJECTED_VALUE_MAX: Final = 200  # AI-CORE-15

_SPACE: Final = re.compile(r"\s+")


class LookalikeGroup(BaseModel):
    """One preferred character and the characters that stand for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    preferred: str = Field(min_length=1, max_length=1)
    variants: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> LookalikeGroup:
        for v in self.variants:
            if len(v) != 1:
                raise ValueError(f"variants must be single characters, not {v!r}")
        if self.preferred in self.variants:
            raise ValueError(f"{_show(self.preferred)} is both preferred and a variant")
        return self


class Lookalikes(BaseModel):
    """A lookalike characters file (``ai.lookalikes``, AI-CORE-22)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    groups: list[LookalikeGroup] = Field(default_factory=list[LookalikeGroup])

    @model_validator(mode="after")
    def _no_overlap(self) -> Lookalikes:
        seen: dict[str, int] = {}
        for i, g in enumerate(self.groups):
            for ch in [g.preferred, *g.variants]:
                if ch in seen and seen[ch] != i:
                    raise ValueError(f"{_show(ch)} is listed in two groups")
                seen[ch] = i
        return self

    def table(self) -> dict[int, str]:
        return {ord(v): g.preferred for g in self.groups for v in g.variants}

    def rewrite(self, text: str) -> str:
        """Each variant replaced by its group's preferred character (AI-CORE-13)."""
        return text.translate(self.table())

    def prompt_line(self) -> str | None:
        """What the request tells the model about the preferred characters (AI-CORE-22)."""
        if not self.groups:
            return None
        parts = [f'"{g.preferred}" instead of "{g.variants[0]}"' for g in self.groups]
        return "In names, write " + "; ".join(parts) + "."


NO_LOOKALIKES: Final = Lookalikes()


def _show(ch: str) -> str:
    return f"{ch!r} (U+{ord(ch):04X})"


def match_key(value: str, lookalikes: Lookalikes = NO_LOOKALIKES) -> str:
    """The key two values match on: NFKC, lookalikes, case folding, whitespace (AI-CORE-13)."""
    text = unicodedata.normalize("NFKC", value)
    text = unicodedata.normalize("NFKC", text.casefold())
    text = lookalikes.rewrite(text)
    return _SPACE.sub(" ", text).strip()


class VocabMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    known: bool
    new: bool = False
    warnings: list[str] = Field(default_factory=list[str])


class Rejection(BaseModel):
    """A value the validation dropped, with why (``ai.rejected``, AI-CORE-15)."""

    model_config = ConfigDict(frozen=True)

    value: str
    reason: str
    field: str | None = None

    @classmethod
    def of(cls, value: str, reason: str, field: str | None = None) -> Rejection:
        return cls(value=value[:REJECTED_VALUE_MAX], reason=reason, field=field)


class VocabularyError(ValueError):
    """Two vocabulary entries share a match key (AI-CORE-13)."""


class Vocabulary:
    """A list of known values, indexed by match key."""

    def __init__(self, entries: Sequence[str], lookalikes: Lookalikes = NO_LOOKALIKES) -> None:
        self.lookalikes = lookalikes
        self._by_key: dict[str, str] = {}
        for entry in entries:
            key = match_key(entry, lookalikes)
            if not key:
                continue
            other = self._by_key.get(key)
            if other is not None and other != entry:
                raise VocabularyError(
                    f"vocabulary entries {other!r} and {entry!r} are the same after "
                    "folding case, spaces and lookalike characters"
                )
            self._by_key.setdefault(key, entry)

    @property
    def entries(self) -> list[str]:
        return list(self._by_key.values())

    def get(self, value: str) -> str | None:
        return self._by_key.get(match_key(value, self.lookalikes))


def match_vocabulary(
    values: Sequence[str],
    vocabulary: Vocabulary,
    mode: MatchMode,
    *,
    field: str | None = None,
) -> tuple[list[VocabMatch], list[Rejection]]:
    """Match answer values against ``vocabulary`` (AI-CORE-13)."""
    out: list[VocabMatch] = []
    rejected: list[Rejection] = []
    seen: set[str] = set()
    for raw in values:
        if not raw.strip():
            rejected.append(Rejection.of(raw, "empty", field))
            continue
        known = vocabulary.get(raw)
        if known is not None:
            match = VocabMatch(value=known, known=True)
        elif mode == "strict":
            rejected.append(Rejection.of(raw, "not_in_vocabulary", field))
            continue
        else:
            kept = _SPACE.sub(" ", vocabulary.lookalikes.rewrite(raw)).strip()
            if mode == "flag":
                match = VocabMatch(value=kept, known=False, warnings=["not_in_vocabulary"])
            else:
                match = VocabMatch(value=kept, known=False, new=True)
        key = match_key(match.value, vocabulary.lookalikes)
        if key in seen:
            continue
        seen.add(key)
        out.append(match)
    return out, rejected
