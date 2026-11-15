"""The art store: cover images by sha256, kept for as long as a ChangeSet may need them
(S-COVERS-SEARCH COVERS-SEARCH-15 to -17).

Chosen covers are put here before their ChangeSet is proposed, and a backend copies an
album's current art here before replacing or removing it, so undo can always restore it.
Unlike the search cache, nothing here is evicted.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from rifferr.core.covers import EXTENSIONS, SHA256, mime_of, sha256_hex
from rifferr.core.snapshot import FieldValue

_CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


class ArtStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, sha: str) -> Path:
        if not SHA256.fullmatch(sha):
            raise ValueError(f"not a sha256: {sha!r}")
        return self.root / sha[:2]

    def find(self, sha: str) -> Path | None:
        folder = self._dir(sha)
        if not folder.is_dir():
            return None
        for ext in (*EXTENSIONS.values(), ".img"):
            p = folder / f"{sha}{ext}"
            if p.is_file():
                return p
        return None

    def has(self, sha: str) -> bool:
        return self.find(sha) is not None

    def put(self, data: bytes) -> tuple[str, Path]:
        sha = sha256_hex(data)
        found = self.find(sha)
        if found is not None:
            return sha, found
        mime = mime_of(data)
        ext = EXTENSIONS[mime] if mime is not None else ".img"
        folder = self._dir(sha)
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / f"{sha}{ext}"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha, dest

    def put_file(self, path: Path) -> str:
        """Copy an image file in (if not there yet); returns its sha256."""
        sha = file_sha256(path)
        if self.find(sha) is not None:
            return sha
        with path.open("rb") as f:
            mime = mime_of(f.read(16))
        ext = EXTENSIONS[mime] if mime is not None else ".img"
        folder = self._dir(sha)
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")
        os.close(fd)
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, folder / f"{sha}{ext}")
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha


def art_problem(scope: str, op: str, value: FieldValue, art: ArtStore | None) -> str | None:
    """COVERS-SEARCH-16: cover changes are album-only and name an image in the store."""
    if art is None:
        return "this library can't set cover art"
    if scope != "album":
        return "cover art belongs to albums"
    if op == "delete":
        return None
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        return "expects the image's sha256"
    if not art.has(value):
        return f"image {value[:12]} isn't in the art store"
    return None
