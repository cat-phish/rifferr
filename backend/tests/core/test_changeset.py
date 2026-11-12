from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from rifferr.core.changeset import (
    ChangeSet,
    ChangeSetProposal,
    ChangeSource,
    FieldChange,
    ProposedFieldChange,
    TargetChange,
    TargetRef,
)
from rifferr.schemas import PUBLISHED

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _changeset(*targets: TargetChange) -> ChangeSet:
    return ChangeSet(
        id="cs1",
        profile="p",
        source=ChangeSource(kind="manual", name="ui"),
        created_at=NOW,
        targets=list(targets),
    )


def test_CHANGESET_1_model_holds_targets_and_field_changes() -> None:
    album = TargetRef(scope="album", id="album-7")
    cs = _changeset(
        TargetChange(
            target=album,
            changes=[FieldChange(field="album", old="Live", new="Live (EP)", rationale="ep")],
        )
    )
    assert cs.run_id is None
    assert cs.step_id is None
    assert cs.field_changes() == [(album, cs.targets[0].changes[0])]
    assert ChangeSet.model_validate_json(cs.model_dump_json()) == cs


def test_CHANGESET_1_changeset_and_targets_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        _changeset()
    with pytest.raises(ValidationError):
        TargetChange(target=TargetRef(scope="item", id="i1"), changes=[])


def test_CHANGESET_1_set_needs_new_and_delete_takes_none() -> None:
    with pytest.raises(ValidationError):
        FieldChange(field="album", op="set")
    with pytest.raises(ValidationError):
        FieldChange(field="album", op="delete", new="x")
    assert FieldChange(field="mood", op="delete", old="calm").new is None


@pytest.mark.parametrize("name", ["Album", "1st", "al-bum", "", "a b", "é"])
def test_CHANGESET_2_field_names_are_checked(name: str) -> None:
    with pytest.raises(ValidationError):
        FieldChange(field=name, new="x")


@pytest.mark.parametrize("name", ["album", "_private", "mb_albumid", "x2"])
def test_CHANGESET_2_valid_field_names(name: str) -> None:
    assert FieldChange(field=name, new="x").field == name


def test_CHANGESET_3_proposal_needs_exactly_one_form() -> None:
    change = {"field": "album", "new": "x"}
    target = {"target": {"scope": "album", "id": "a"}, "changes": [change]}
    assert ChangeSetProposal.model_validate({"changes": [change]}).targets is None
    assert ChangeSetProposal.model_validate({"targets": [target]}).changes is None
    with pytest.raises(ValidationError):
        ChangeSetProposal.model_validate({})
    with pytest.raises(ValidationError):
        ChangeSetProposal.model_validate({"changes": [change], "targets": [target]})


def test_CHANGESET_3_old_may_be_omitted_and_is_distinguished_from_null() -> None:
    assert not ProposedFieldChange(field="album", new="x").has_old()
    assert ProposedFieldChange(field="album", new="x", old=None).has_old()


def test_CHANGESET_3_proposal_schema_is_published() -> None:
    schema = PUBLISHED["changeset-proposal"].model_json_schema()
    assert set(schema["properties"]) == {"rationale", "targets", "changes"}
    assert schema["oneOf"] == [{"required": ["targets"]}, {"required": ["changes"]}]


def test_CHANGESET_9_subset_keeps_accepted_field_changes_in_order() -> None:
    album, item = TargetRef(scope="album", id="a"), TargetRef(scope="item", id="i")
    cs = _changeset(
        TargetChange(target=album, changes=[FieldChange(field="album", new="A")]),
        TargetChange(
            target=item,
            changes=[FieldChange(field="title", new="T"), FieldChange(field="mood", new="M")],
        ),
    )
    sub = cs.subset([2, 0])
    assert [(t.label(), c.field) for t, c in sub.field_changes()] == [
        ("album a", "album"),
        ("item i", "mood"),
    ]
    assert sub.id == cs.id
    with pytest.raises(ValueError, match="at least one"):
        cs.subset([])
    with pytest.raises(ValueError, match="out of range"):
        cs.subset([3])
