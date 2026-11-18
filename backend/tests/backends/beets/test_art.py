"""Cover art through ChangeSets in BeetsBackend (S-COVERS-SEARCH COVERS-SEARCH-16, -17, -22)."""

from __future__ import annotations

from pathlib import Path

from rifferr.artstore import ArtStore
from rifferr.backends.beets import BeetsBackend
from rifferr.core.changeset import FieldChange, TargetRef
from rifferr.core.covers import ART_FIELD, sha256_hex
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot
from tests.backends.beets.conftest import BeetsEnv
from tests.backends.beets.test_apply import album_ref, changeset, imported
from tests.covers.images import jpeg, png


def setup(env: BeetsEnv) -> tuple[BeetsBackend, ArtStore, AlbumSnapshot]:
    backend = env.backend(art=True)
    assert backend.art is not None
    return backend, backend.art, imported(env, backend)


def art_of(backend: BeetsBackend) -> tuple[str | None, str | None]:
    (album,) = backend.query(Selection())
    sha = album.fields.get(ART_FIELD)
    path = album.fields.get("artpath")
    return (sha if isinstance(sha, str) else None, path if isinstance(path, str) else None)


def set_art(ref: TargetRef, old: str | None, new: str | None) -> FieldChange:
    if new is None:
        return FieldChange(field=ART_FIELD, op="delete", old=old)
    return FieldChange(field=ART_FIELD, old=old, new=new)


def test_COVERS_SEARCH_16_set_art_places_the_file_and_snapshots_show_it(
    beets_env: BeetsEnv,
) -> None:
    backend, store, album = setup(beets_env)
    assert backend.capabilities.art
    assert ART_FIELD not in album.fields
    sha, _ = store.put(jpeg(800, 800))
    result = backend.apply(changeset(album_ref(album), set_art(album_ref(album), None, sha)))
    assert result.applied
    assert [(e.field, e.before, e.after) for e in result.effects] == [(ART_FIELD, None, sha)]
    now, path = art_of(backend)
    assert now == sha
    assert path is not None
    assert Path(path).parent == Path(album.path or "")
    assert Path(path).name == "cover.jpg"  # beets' art_filename default + the extension


def test_COVERS_SEARCH_16_replacing_art_keeps_the_old_image_in_the_store(
    beets_env: BeetsEnv,
) -> None:
    backend, store, album = setup(beets_env)
    ref = album_ref(album)
    first, _ = store.put(jpeg(800, 800, b"one"))
    second, _ = store.put(jpeg(900, 900, b"two"))
    backend.apply(changeset(ref, set_art(ref, None, first)))
    _, path = art_of(backend)
    assert path is not None
    store_dir = store.find(first)
    assert store_dir is not None
    store_dir.unlink()  # only the album folder has it now
    backend.apply(changeset(ref, set_art(ref, first, second)))
    assert art_of(backend)[0] == second
    assert store.has(first)  # copied in before beets replaced it


def test_COVERS_SEARCH_16_a_changed_cover_is_a_conflict(beets_env: BeetsEnv) -> None:
    backend, store, album = setup(beets_env)
    ref = album_ref(album)
    sha, _ = store.put(png(600, 600))
    result = backend.apply(changeset(ref, set_art(ref, "0" * 64, sha)))
    assert not result.applied
    assert [c.field for c in result.conflicts] == [ART_FIELD]
    assert art_of(backend) == (None, None)


def test_COVERS_SEARCH_16_check_refuses_items_and_images_not_stored(beets_env: BeetsEnv) -> None:
    backend, _, album = setup(beets_env)
    item = TargetRef(scope="item", id=album.items[0].id)
    missing = sha256_hex(b"nothing")
    problems = backend.check(changeset(item, set_art(item, None, missing)))
    assert [p.message for p in problems] == ["cover art belongs to albums"]
    problems = backend.check(changeset(album_ref(album), set_art(album_ref(album), None, missing)))
    assert problems[0].message.endswith("isn't in the art store")
    plain = beets_env.backend()
    assert not plain.capabilities.art
    problems = plain.check(changeset(album_ref(album), set_art(album_ref(album), None, missing)))
    assert [p.message for p in problems] == ["this library can't set cover art"]


def test_COVERS_SEARCH_17_undo_restores_the_old_art_or_removes_it(beets_env: BeetsEnv) -> None:
    backend, store, album = setup(beets_env)
    ref = album_ref(album)
    first, _ = store.put(jpeg(800, 800, b"one"))
    second, _ = store.put(png(900, 900, b"two"))
    backend.apply(changeset(ref, set_art(ref, None, first)))
    backend.apply(changeset(ref, set_art(ref, first, second)))
    _, png_path = art_of(backend)
    assert png_path is not None
    assert png_path.endswith("cover.png")
    # Undo builds the inverse: after -> before.
    backend.apply(changeset(ref, set_art(ref, second, first)))
    sha, path = art_of(backend)
    assert sha == first
    assert path is not None
    assert path.endswith("cover.jpg")
    backend.apply(changeset(ref, set_art(ref, first, None)))
    assert art_of(backend) == (None, None)
    assert not Path(path).exists()
    assert store.has(first)  # removed from the folder, never lost


def test_COVERS_SEARCH_16_an_image_beets_would_overwrite_is_kept(beets_env: BeetsEnv) -> None:
    backend, store, album = setup(beets_env)
    ref = album_ref(album)
    stray = Path(album.path or "") / "cover.jpg"
    stray.write_bytes(jpeg(500, 500, b"user's own"))  # not known to beets
    sha, _ = store.put(jpeg(1000, 1000))
    backend.apply(changeset(ref, set_art(ref, None, sha)))
    assert store.has(sha256_hex(jpeg(500, 500, b"user's own")))
    assert art_of(backend)[0] == sha
