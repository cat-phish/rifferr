from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot

field_values = st.one_of(
    st.text(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    st.lists(st.text(), max_size=3),
)
fields = st.dictionaries(st.text(min_size=1, max_size=12), field_values, max_size=6)
items = st.builds(ItemSnapshot, id=st.text(min_size=1), path=st.text(min_size=1), fields=fields)
albums = st.builds(
    AlbumSnapshot,
    id=st.text(min_size=1),
    path=st.none() | st.text(min_size=1),
    fields=fields,
    items=st.lists(items, max_size=3),
)


@given(albums)
def test_BACKEND_4_album_snapshot_json_round_trip_is_lossless(album: AlbumSnapshot) -> None:
    again = AlbumSnapshot.model_validate_json(album.model_dump_json())
    assert again == album
    for key, value in album.fields.items():
        assert type(again.fields[key]) is type(value)


def test_BACKEND_3_snapshots_are_immutable() -> None:
    item = ItemSnapshot(id="i1", path="/x.flac", fields={"title": "x"})
    with pytest.raises(ValidationError):
        item.id = "other"  # type: ignore[misc]


def test_BACKEND_3_field_values_are_restricted() -> None:
    with pytest.raises(ValidationError):
        ItemSnapshot(id="i1", path="/x", fields={"bad": {"nested": 1}})  # type: ignore[dict-item]
    with pytest.raises(ValidationError):
        ItemSnapshot(id="i1", path="/x", fields={"bad": float("nan")})


def test_BACKEND_5_selection_defaults() -> None:
    sel = Selection()
    assert (sel.query, sel.scope, sel.run_id) == ("", "album", None)
    assert Selection.for_run("r1") == Selection(run_id="r1")


def test_BACKEND_5_selection_rejects_unknown_scope() -> None:
    with pytest.raises(ValidationError):
        Selection(scope="track")  # type: ignore[arg-type]
