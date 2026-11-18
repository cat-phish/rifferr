from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.base import LibraryBackend
from rifferr.backends.beets import BeetsBackend
from rifferr.core.changeset import FieldChange, TargetRef
from rifferr.core.covers import ART_FIELD
from tests.backends.beets.conftest import BeetsEnv, seed
from tests.backends.contract import BackendContract, changeset


class TestBeetsBackendContract(BackendContract):
    """BEETS-IMPORT-11: the shared contract suite (BACKEND-8) against real beets."""

    @pytest.fixture(autouse=True)
    def _env(self, beets_env: BeetsEnv) -> None:
        self.env = beets_env

    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
        backend = self.env.backend(art=True)
        seed(backend, albums)
        return backend

    def read_only(self, backend: LibraryBackend) -> LibraryBackend:
        return BeetsBackend(self.env.beetsdir.resolve(), read_only=True)

    def test_COVERS_SEARCH_18_art_is_set_kept_on_the_album_and_removed(
        self, backend: LibraryBackend
    ) -> None:
        """Seeded albums have no folders: beets refuses their art (test_art.py covers it)."""
        album = self.by_album(backend, "First EP")
        ref = TargetRef(scope="album", id=album.id)
        sha = self.store_image(backend, b"\xff\xd8\xff\xe0 contract image")
        problems = backend.check(changeset((ref, [FieldChange(field=ART_FIELD, new=sha)])))
        assert [p.message for p in problems] == ["the album's folder doesn't exist"]


def test_BEETS_IMPORT_11_contract_runs_for_declared_capabilities(beets_env: BeetsEnv) -> None:
    """TestBeetsBackendContract above runs the shared suite; pin what it covers."""
    from rifferr.backends.base import BackendCapabilities

    backend = beets_env.backend()
    assert isinstance(backend, LibraryBackend)
    assert backend.capabilities == BackendCapabilities(
        import_=True,
        query=True,
        templates=True,
        apply=True,
        command_env=True,
        cli=True,
        import_decisions=True,
        files=True,
        remove=True,
        move=True,
    )
    assert beets_env.backend(art=True).capabilities.art
    assert issubclass(TestBeetsBackendContract, BackendContract)
