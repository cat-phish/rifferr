"""BeetsBackend.remove_album: removes an imported album, deleting its files only when
asked (F-DASHBOARD DASHBOARD-7, -18)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rifferr.backends.base import CapabilityError
from rifferr.backends.beets import BeetsBackend
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BeetsEnv


def test_DASHBOARD_7_remove_album_keeps_files_by_default(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    backend.import_folder(beets_env.inbox_album(), "quiet", None, run_id="01RUN")
    (album,) = backend.query(Selection())
    paths = [Path(i.path) for i in album.items]
    assert backend.remove_album(album.id, delete_files=False) == []
    assert backend.query(Selection()) == []
    assert all(p.exists() for p in paths)


def test_DASHBOARD_18_remove_album_deletes_files_and_empty_folders(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    backend.import_folder(beets_env.inbox_album(), "quiet", None, run_id="01RUN")
    (album,) = backend.query(Selection())
    paths = [Path(i.path) for i in album.items]
    library = backend.library_dirs()[0]
    assert all(p.is_relative_to(library) for p in paths)
    deleted = backend.remove_album(album.id, delete_files=True)
    assert sorted(deleted) == sorted(str(p) for p in paths)
    assert not any(p.exists() for p in paths)
    assert not paths[0].parent.exists()  # beets prunes the emptied album folder
    assert library.exists()


def test_DASHBOARD_7_remove_album_unknown_or_read_only(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    with pytest.raises(KeyError):
        backend.remove_album("album:999", delete_files=False)
    read_only = BeetsBackend(backend.beetsdir, read_only=True)
    with pytest.raises(CapabilityError):
        read_only.remove_album("album:1", delete_files=False)
