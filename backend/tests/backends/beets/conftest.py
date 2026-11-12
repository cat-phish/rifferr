"""Temp beets profiles for BeetsBackend tests (BEETS-IMPORT-12).

Everything lives under tmp_path: BEETSDIR, library DB and music directory. The
config turns autotagging off so no test talks to MusicBrainz.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from beets.library import Album, Item

from rifferr.backends.beets import BeetsBackend, config

FIXTURES = Path(__file__).parents[2] / "fixtures" / "audio"

BASE_CONFIG = """\
directory: {music}
library: {db}
plugins: []
threaded: no
create_backup_before_migrations: no
import:
  autotag: no
  copy: yes
  write: no
  log: {log}
"""


class BeetsEnv:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.beetsdir = root / "beets"
        self.music = root / "library"
        self.inbox = root / "inbox"
        for d in (self.beetsdir, self.music, self.inbox):
            d.mkdir(parents=True)

    def write_config(self, extra: str = "") -> Path:
        path = self.beetsdir / "config.yaml"
        text = BASE_CONFIG.format(
            music=self.music, db=self.beetsdir / "library.db", log=self.beetsdir / "import.log"
        )
        path.write_text(text + extra)
        return path

    def backend(self, extra: str = "") -> BeetsBackend:
        self.write_config(extra)
        config.load(self.beetsdir)
        return BeetsBackend(self.beetsdir.resolve())

    def inbox_album(self, fixture: str = "album-flac", name: str | None = None) -> Path:
        dest = self.inbox / (name or fixture)
        shutil.copytree(FIXTURES / fixture, dest)
        return dest


@pytest.fixture
def beets_env(tmp_path: Path) -> Iterator[BeetsEnv]:
    config.reset()
    yield BeetsEnv(tmp_path)
    config.reset()


def seed(backend: BeetsBackend, albums: list[dict[str, Any]]) -> None:
    """Add albums without audio files (paths are fake), with fields and flexible attributes."""
    with backend.library() as lib:
        for spec in albums:
            fields = {k: v for k, v in spec.items() if k != "items"}
            items = []
            for n, item_spec in enumerate(spec["items"]):
                item = Item(
                    path=f"/fake/{fields['album']}/{n}.flac".encode(),
                    album=fields["album"],
                    albumartist=fields["albumartist"],
                    **item_spec,
                )
                items.append(item)
            album: Album = lib.add_album(items)
            for key, value in fields.items():
                album[key] = value
            album.store()
