from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.base import LibraryBackend
from rifferr.backends.beets import BeetsBackend
from tests.backends.beets.conftest import BeetsEnv, seed
from tests.backends.contract import BackendContract


class TestBeetsBackendContract(BackendContract):
    """BEETS-IMPORT-11: the shared contract suite (BACKEND-8) against real beets."""

    @pytest.fixture(autouse=True)
    def _env(self, beets_env: BeetsEnv) -> None:
        self.env = beets_env

    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
        backend = self.env.backend()
        seed(backend, albums)
        return backend

    def read_only(self, backend: LibraryBackend) -> LibraryBackend:
        return BeetsBackend(self.env.beetsdir.resolve(), read_only=True)


def test_BEETS_IMPORT_11_contract_runs_for_declared_capabilities(beets_env: BeetsEnv) -> None:
    """TestBeetsBackendContract above runs the shared suite; pin what it covers."""
    from rifferr.backends.base import BackendCapabilities

    backend = beets_env.backend()
    assert isinstance(backend, LibraryBackend)
    assert backend.capabilities == BackendCapabilities(
        import_=True, query=True, templates=True, apply=True, command_env=True
    )
    assert issubclass(TestBeetsBackendContract, BackendContract)
