"""C-FILE-TAGS: flexible attributes written as file tags, through beets' own write path.

Contract tests (FILE-TAGS-8): what mutagen sees in each format after beets writes,
reads back and deletes a mapped field.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import mutagen
import pytest
from beets.library import Item

from rifferr.backends.beets import config, file_tags
from tests.backends.beets.conftest import FIXTURES, BeetsEnv

FILE_TAGS = """
rifferr:
  file_tags:
    releasedate: RELEASEDATE
    date_added: DATE ADDED
    subgenres: { tag: SUBGENRE, multi: true }
"""


def track(env: BeetsEnv, fixture: str, suffix: str) -> Path:
    src = next((FIXTURES / fixture).glob(f"*{suffix}"))
    dest = env.root / f"t{suffix}"
    shutil.copy(src, dest)
    return dest


def raw(path: Path) -> dict[str, list[str]]:
    f = mutagen.File(path)  # pyright: ignore[reportPrivateImportUsage]
    assert f is not None
    assert f.tags is not None
    out: dict[str, list[str]] = {}
    if path.suffix == ".mp3":
        for frame in f.tags.getall("TXXX"):
            out[frame.desc] = list(frame.text)
    elif path.suffix == ".m4a":
        for key, value in f.tags.items():
            if key.startswith(file_tags.MP4_FREEFORM):
                out[key[len(file_tags.MP4_FREEFORM) :]] = [bytes(v).decode() for v in value]
    else:
        for key, value in f.tags.items():
            out[key.upper()] = list(value)
    return out


FORMATS = [("album-flac", ".flac"), ("single-mp3", ".mp3"), ("single-m4a", ".m4a")]


@pytest.mark.parametrize(("fixture", "suffix"), FORMATS)
def test_FILE_TAGS_1_FILE_TAGS_3_FILE_TAGS_8_write_and_read_back(
    beets_env: BeetsEnv, fixture: str, suffix: str
) -> None:
    beets_env.backend(FILE_TAGS)
    path = track(beets_env, fixture, suffix)
    item = Item.from_path(path)
    item["releasedate"] = "2004-03-22"
    item["subgenres"] = ["Post-Rock", "Math Rock"]
    item.write()
    tags = raw(path)
    assert tags["RELEASEDATE"] == ["2004-03-22"]
    assert tags["SUBGENRE"] == ["Post-Rock", "Math Rock"]
    assert "DATE ADDED" not in tags
    again = Item.from_path(path)
    assert again["releasedate"] == "2004-03-22"
    assert again["subgenres"] == ["Post-Rock", "Math Rock"]


def test_FILE_TAGS_2_per_format_override(beets_env: BeetsEnv) -> None:
    beets_env.backend("rifferr:\n  file_tags:\n    mood_note: { tag: MOODNOTE, id3: NOTE }\n")
    mp3 = track(beets_env, "single-mp3", ".mp3")
    flac = track(beets_env, "album-flac", ".flac")
    for path in (mp3, flac):
        item = Item.from_path(path)
        item["mood_note"] = "calm"
        item.write()
    assert raw(mp3) == {"NOTE": ["calm"]}
    assert raw(flac)["MOODNOTE"] == ["calm"]


@pytest.mark.parametrize(("fixture", "suffix"), FORMATS)
def test_FILE_TAGS_6_empty_field_removes_tag(
    beets_env: BeetsEnv, fixture: str, suffix: str
) -> None:
    beets_env.backend(FILE_TAGS)
    path = track(beets_env, fixture, suffix)
    item = Item.from_path(path)
    item["releasedate"] = "2004"
    item["subgenres"] = ["Rock"]
    item.write()
    del item["releasedate"]
    item["subgenres"] = []
    item.write()
    tags = raw(path)
    assert "RELEASEDATE" not in tags
    assert "SUBGENRE" not in tags


def test_FILE_TAGS_10_id3v23_joins_values(beets_env: BeetsEnv) -> None:
    beets_env.backend(FILE_TAGS + "id3v23: yes\n")
    path = track(beets_env, "single-mp3", ".mp3")
    item = Item.from_path(path)
    item["subgenres"] = ["Post-Rock", "Math Rock"]
    item.write()
    assert raw(path)["SUBGENRE"] == ["Post-Rock; Math Rock"]
    assert Item.from_path(path)["subgenres"] == ["Post-Rock", "Math Rock"]


def test_FILE_TAGS_9_multi_field_is_a_list_type(beets_env: BeetsEnv) -> None:
    beets_env.backend(FILE_TAGS)
    assert Item._type("subgenres").model_type is list  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("genres: GENRE2", "already a media file field"),
        ("album_id: X", "fixed field"),
        ("foo: 'A=B'", "usable tag name"),
        ("foo: {tag: X, bogus: 1}", "foo"),
    ],
)
def test_FILE_TAGS_4_bad_entries_stop_startup(
    beets_env: BeetsEnv, entry: str, message: str
) -> None:
    with pytest.raises(config.BeetsConfigError, match=message):
        beets_env.backend(f"rifferr:\n  file_tags:\n    {entry}\n")
    assert file_tags.registered() == {}


def test_FILE_TAGS_1_changeset_on_album_writes_mapped_tags(beets_env: BeetsEnv) -> None:
    from rifferr.core.changeset import FieldChange
    from tests.backends.beets.test_apply import album_ref, changeset, imported

    backend = beets_env.backend(FILE_TAGS)
    album = imported(beets_env, backend)
    assert "releasedate" not in album.fields  # FILE-TAGS-5: no tag, no field
    result = backend.apply(
        changeset(
            album_ref(album),
            FieldChange(field="releasedate", old=None, new="2004-03-22"),
            FieldChange(field="subgenres", old=None, new=["Post-Rock", "Math Rock"]),
        )
    )
    assert result.applied
    assert result.write_failures == []
    for item in album.items:
        tags = raw(Path(item.path))
        assert tags["RELEASEDATE"] == ["2004-03-22"]
        assert tags["SUBGENRE"] == ["Post-Rock", "Math Rock"]


def test_FILE_TAGS_5_import_reads_mapped_tags(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend(FILE_TAGS)
    folder = beets_env.inbox_album()
    for path in folder.glob("*.flac"):
        item = Item.from_path(path)
        item["subgenres"] = ["Shoegaze", "Dream Pop"]
        item.write()
    backend.import_folder(folder, "quiet", None, run_id="01RUN")
    from rifferr.core.selection import Selection

    (album,) = backend.query(Selection())
    assert [i.fields["subgenres"] for i in album.items] == [["Shoegaze", "Dream Pop"]] * 2


def test_FILE_TAGS_7_library_page_lists_file_tag_fields(beets_env: BeetsEnv) -> None:
    from rifferr.engine.manual import LibraryQuery, library_page

    backend = beets_env.backend(FILE_TAGS)
    assert library_page(backend, LibraryQuery()).file_tags == [
        "date_added",
        "releasedate",
        "subgenres",
    ]
