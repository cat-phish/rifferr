"""F-TAG-GRID library page, edit check and manual ChangeSets (TAG-GRID-2, -5, -7, -8)."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from pydantic import ValidationError

from rifferr.backends.base import QueryError
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import FieldChange, TargetRef
from rifferr.core.selection import Selection
from rifferr.engine.manual import (
    MANUAL_SOURCE,
    LibraryQuery,
    ManualChange,
    ManualChanges,
    check_manual,
    library_page,
    manual_changesets,
)


def library() -> FakeBackend:
    fb = FakeBackend()
    for n, name in enumerate(["First", "Second", "Third"]):
        fb.add_album(
            {
                "albumartist": "Band",
                "album": name,
                "year": 2000 + n,
                "length": 200.0,
                "genres": ["Rock"],
                "items": [{"title": f"{name} {t}", "track": t} for t in (1, 2)],
            }
        )
    return fb


def ids() -> Any:
    numbers = count(1)
    return lambda: f"cs-{next(numbers)}"


def album_edit(fb: FakeBackend, name: str, *changes: FieldChange) -> ManualChange:
    album = next(a for a in fb.query(Selection()) if a.fields["album"] == name)
    return ManualChange(
        target=TargetRef(scope="album", id=album.id), album=album.id, changes=list(changes)
    )


# -- TAG-GRID-2: library page ---------------------------------------------------------


def test_TAG_GRID_2_album_scope_page_with_field_names_and_limit() -> None:
    fb = library().read_only_view()
    page = library_page(fb, LibraryQuery())
    assert [a.fields["album"] for a in page.albums] == ["First", "Second", "Third"]
    assert not page.truncated
    assert page.album_fields == ["album", "albumartist", "genres", "length", "year"]
    assert "title" in page.item_fields
    assert "track" in page.item_fields
    page = library_page(fb, LibraryQuery(query="album:s", limit=1))
    assert [a.fields["album"] for a in page.albums] == ["First"]
    assert page.truncated


def test_TAG_GRID_2_item_scope_counts_items_against_the_limit() -> None:
    fb = library().read_only_view()
    page = library_page(fb, LibraryQuery(scope="item", query="title:1"))
    assert [len(a.items) for a in page.albums] == [1, 1, 1]
    page = library_page(fb, LibraryQuery(scope="item", limit=3))
    assert [len(a.items) for a in page.albums] == [2, 1]
    assert page.truncated


def test_TAG_GRID_2_bad_query_and_limit_bounds() -> None:
    with pytest.raises(QueryError):
        library_page(library(), LibraryQuery(query="bare-term"))
    assert LibraryQuery().limit == 1000
    with pytest.raises(ValidationError):
        LibraryQuery(limit=10001)


def test_TAG_GRID_2_reading_writes_nothing() -> None:
    fb = library()
    library_page(fb, LibraryQuery())
    assert fb.apply_calls == []


# -- TAG-GRID-7: check ---------------------------------------------------------------


def test_TAG_GRID_7_values_are_coerced_to_the_field_type() -> None:
    fb = library()
    edit = album_edit(fb, "First", FieldChange(field="year", old=2000, new="2001"))
    result = check_manual(fb.read_only_view(), ManualChanges(targets=[edit]))
    assert result.problems == []
    (change,) = result.targets[0].changes
    assert change.new == 2001


def test_TAG_GRID_7_problems_per_target_and_field() -> None:
    fb = library()
    bad_year = album_edit(fb, "First", FieldChange(field="year", old=2000, new="soon"))
    read_only = album_edit(fb, "Second", FieldChange(field="path", old=None, new="/x"))
    gone = ManualChange(
        target=TargetRef(scope="album", id="album-999"),
        album="album-999",
        changes=[FieldChange(field="album", new="X")],
    )
    result = check_manual(fb.read_only_view(), ManualChanges(targets=[bad_year]))
    (p,) = result.problems
    assert (p.target, p.field) == (bad_year.target, "year")
    assert "whole number" in p.message
    result = check_manual(fb.read_only_view(), ManualChanges(targets=[read_only, gone]))
    assert {(p.target.id, p.field) for p in result.problems} == {
        (read_only.target.id, "path"),
        ("album-999", ""),
    }


def test_TAG_GRID_7_old_values_from_json_are_coerced_too() -> None:
    """A browser sends ``200.0`` back as ``200``; it must not look like a conflict."""
    fb = library()
    edit = album_edit(fb, "First", FieldChange(field="length", old=200, new="201.5"))
    (change,) = check_manual(fb, ManualChanges(targets=[edit])).targets[0].changes
    assert change.old == 200.0
    assert isinstance(change.old, float)
    assert change.new == 201.5


def test_TAG_GRID_5_multi_value_text_is_one_entry_never_split() -> None:
    fb = library()
    edit = album_edit(fb, "First", FieldChange(field="genres", old=["Rock"], new="Rock; Pop"))
    (change,) = check_manual(fb, ManualChanges(targets=[edit])).targets[0].changes
    assert change.new == ["Rock; Pop"]
    edit = album_edit(fb, "First", FieldChange(field="genres", old=["Rock"], new=["Rock", "Pop"]))
    (change,) = check_manual(fb, ManualChanges(targets=[edit])).targets[0].changes
    assert change.new == ["Rock", "Pop"]


def test_TAG_GRID_7_check_changes_nothing() -> None:
    fb = library()
    edit = album_edit(fb, "First", FieldChange(field="year", old=2000, new="2001"))
    check_manual(fb.read_only_view(), ManualChanges(targets=[edit]))
    assert fb.apply_calls == []
    assert fb.query(Selection(query="album:First"))[0].fields["year"] == 2000


# -- TAG-GRID-8: manual ChangeSets -----------------------------------------------------


def test_TAG_GRID_8_one_manual_changeset_per_album() -> None:
    fb = library()
    first, second = fb.query(Selection())[:2]
    item = first.items[0]
    request = ManualChanges(
        targets=[
            album_edit(fb, "First", FieldChange(field="year", old=2000, new=2001)),
            ManualChange(
                target=TargetRef(scope="item", id=item.id),
                album=first.id,
                changes=[FieldChange(field="title", old="First 1", new="Intro")],
            ),
            album_edit(fb, "Second", FieldChange(field="genres", op="delete", old=["Rock"])),
        ],
        rationale="fix years",
    )
    now = datetime(2026, 11, 12, tzinfo=UTC)
    out = manual_changesets(request, make_id=ids(), profile="main", now=lambda: now)
    assert [c.id for c in out] == ["cs-1", "cs-2"]
    assert all(c.source == MANUAL_SOURCE and c.run_id is None and c.step_id is None for c in out)
    assert [len(c.targets) for c in out] == [2, 1]
    assert out[0].targets[1].target.id == item.id
    assert out[1].targets[0].target.id == second.id
    assert out[0].rationale == "fix years"
    assert out[0].targets[0].changes[0].old == 2000  # the value the user saw


def test_TAG_GRID_8_no_op_edits_are_dropped() -> None:
    fb = library()
    request = ManualChanges(
        targets=[
            album_edit(fb, "First", FieldChange(field="year", old=2000, new=2000)),
            album_edit(fb, "Second", FieldChange(field="label", op="delete", old=None)),
        ]
    )
    assert manual_changesets(request, make_id=ids(), profile="main", now=datetime.now) == []


def test_TAG_GRID_8_edits_are_validated() -> None:
    target = TargetRef(scope="album", id="album-1")
    with pytest.raises(ValidationError, match="its own id"):
        ManualChange(target=target, album="album-2", changes=[FieldChange(field="a", new="b")])
    with pytest.raises(ValidationError, match="changed once"):
        ManualChange(
            target=target,
            album="album-1",
            changes=[FieldChange(field="a", new="b"), FieldChange(field="a", new="c")],
        )
    one = ManualChange(target=target, album="album-1", changes=[FieldChange(field="a", new="b")])
    with pytest.raises(ValidationError, match="appear once"):
        ManualChanges(targets=[one, one])
