"""BeetsBackend.check/apply against a temp library with real FLAC files (CHANGESET-10, -11)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from mediafile import MediaFile

from rifferr.backends.beets import BeetsBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSource,
    FieldChange,
    TargetChange,
    TargetRef,
)
from rifferr.core.profile import Profile
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot
from tests.backends.beets.conftest import BeetsEnv


def changeset(target: TargetRef, *changes: FieldChange) -> ChangeSet:
    return ChangeSet(
        id="cs-beets",
        profile="test",
        source=ChangeSource(kind="manual", name="test"),
        created_at=datetime(2026, 11, 12, tzinfo=UTC),
        targets=[TargetChange(target=target, changes=list(changes))],
    )


def imported(env: BeetsEnv, backend: BeetsBackend) -> AlbumSnapshot:
    backend.import_folder(env.inbox_album(), "quiet", None, run_id="01RUN")
    (album,) = backend.query(Selection())
    return album


def album_ref(album: AlbumSnapshot) -> TargetRef:
    return TargetRef(scope="album", id=album.id)


def test_CHANGESET_10_album_change_is_written_to_every_file(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    result = backend.apply(
        changeset(album_ref(album), FieldChange(field="album", old="Fixture Album", new="Renamed"))
    )
    assert result.applied
    assert result.write_failures == []
    for item in album.items:
        assert MediaFile(item.path).album == "Renamed"
    assert {e.target.scope for e in result.effects} == {"album", "item"}


def test_CHANGESET_10_item_change_is_written_to_that_file_only(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    first, second = album.items
    result = backend.apply(
        changeset(
            TargetRef(scope="item", id=first.id),
            FieldChange(field="title", old="First", new="Opening"),
        )
    )
    assert result.applied
    assert MediaFile(first.path).title == "Opening"
    assert MediaFile(second.path).title == "Second"


def test_CHANGESET_10_write_tags_false_changes_only_the_database(
    beets_env: BeetsEnv,
) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    db_only = BeetsBackend(backend.beetsdir, write_tags=False)
    result = db_only.apply(
        changeset(album_ref(album), FieldChange(field="album", old="Fixture Album", new="DB"))
    )
    assert result.applied
    assert db_only.query(Selection())[0].fields["album"] == "DB"
    assert MediaFile(album.items[0].path).album == "Fixture Album"


def test_CHANGESET_10_from_profile_reads_write_tags(beets_env: BeetsEnv, tmp_path: Path) -> None:
    from rifferr.backends.beets import from_profile
    from rifferr.core.profile import ChangeSettings

    beets_env.write_config()
    profile = Profile.model_validate(
        {
            "name": "p",
            "root": tmp_path,
            "beets": {"beetsdir": str(beets_env.beetsdir)},
            "changes": ChangeSettings(write_tags=False),
        }
    )
    assert from_profile(profile).write_tags is False


def test_CHANGESET_10_failed_file_write_keeps_the_db_change_and_is_reported(
    beets_env: BeetsEnv,
) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    broken = album.items[0]
    os.remove(broken.path)
    result = backend.apply(
        changeset(album_ref(album), FieldChange(field="album", old="Fixture Album", new="Kept"))
    )
    assert result.applied
    (failure,) = result.write_failures
    assert failure.target.id == broken.id
    assert failure.path == broken.path
    assert failure.error
    assert backend.query(Selection())[0].fields["album"] == "Kept"
    assert MediaFile(album.items[1].path).album == "Kept"


def test_CHANGESET_11_flexible_album_attribute_reaches_items(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    result = backend.apply(
        changeset(album_ref(album), FieldChange(field="mood", old=None, new="calm"))
    )
    items = {e.target.id for e in result.effects if e.target.scope == "item"}
    assert items == {i.id for i in album.items}
    assert all(not e.existed_before for e in result.effects)
    after = backend.query(Selection())[0]
    assert after.fields["mood"] == "calm"
    assert [i.fields["mood"] for i in after.items] == ["calm", "calm"]


def test_CHANGESET_11_effects_leave_out_bookkeeping_fields(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    result = backend.apply(
        changeset(album_ref(album), FieldChange(field="album", old="Fixture Album", new="X"))
    )
    assert {e.field for e in result.effects} == {"album"}


def test_CHANGESET_2_computed_fields_are_read_only(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    item = TargetRef(scope="item", id=album.items[0].id)
    problems = backend.check(changeset(item, FieldChange(field="singleton", new=True)))
    assert [p.field for p in problems] == ["singleton"]


def test_CHANGESET_2_untyped_flexible_attributes_take_strings(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    album = imported(beets_env, backend)
    ref = album_ref(album)
    assert backend.check(changeset(ref, FieldChange(field="mood", new="calm"))) == []
    (problem,) = backend.check(changeset(ref, FieldChange(field="mood", new=3)))
    assert "str" in problem.message
