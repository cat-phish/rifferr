from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.base import LibraryBackend
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
