"""Review gates (S-REVIEW): a human checkpoint that holds a suspended run (RUN-14).

A gate's content is fixed when it is created (REVIEW-6), so a decision always
refers to what the reviewer saw.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from rifferr.core.changeset import FieldEffect, TargetRef
from rifferr.core.snapshot import FieldValue

GateState = Literal["pending", "continued", "rejected", "cancelled"]
GateDecision = Literal["continue", "reject"]
OnReject = Literal["stop", "undo"]

#: ``run.finished`` reason of a run rejected at a gate (REVIEW-8).
REJECTED_AT_REVIEW = "rejected_at_review"

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class GateAlbum(BaseModel):
    """One album as the reviewer sees it at the gate (REVIEW-3)."""

    model_config = _FROZEN

    target: TargetRef
    label: str
    message: str | None = None
    shown: dict[str, FieldValue] = Field(default_factory=dict[str, FieldValue])
    changes: list[FieldEffect] = Field(
        default_factory=list[FieldEffect],
        description="The run's changes to the album and its items so far (REVIEW-4)",
    )
    conflicted: list[str] = Field(
        default_factory=list[str], description="Ids of the run's conflicted ChangeSets"
    )
    imported_by_run: bool = False


class ReviewGate(BaseModel):
    model_config = _FROZEN

    id: str
    profile: str
    run_id: str
    step_id: str
    state: GateState = "pending"
    albums: list[GateAlbum]
    on_reject: OnReject = "stop"
    on_timeout: GateDecision | None = None
    """Decision taken once ``deadline`` passes (REVIEW-9)."""
    created_at: datetime
    deadline: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    """``"ui"``, ``"cli"``, a user, or ``"timeout"``."""
    note: str | None = None


class GateRequest(BaseModel):
    """What a step hands ``ctx.open_gate`` for one album; the engine adds the rest."""

    model_config = _FROZEN

    album_id: str
    message: str | None = None
    shown: dict[str, FieldValue] = Field(default_factory=dict[str, FieldValue])
