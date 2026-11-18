"""The cover search cache (S-COVERS-SEARCH COVERS-SEARCH-11).

Search results per provider and normalised query, for ``cache_ttl``; images and
thumbnails by URL; and the URL of every candidate seen, so the API can serve a
candidate's image by its id. The total size is capped, least recently used out first.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from rifferr.core.covers import CoverCandidate


def _now() -> datetime:
    return datetime.now(UTC)


def _key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class CoverCache:
    def __init__(
        self,
        root: Path,
        *,
        ttl: timedelta,
        max_bytes: int,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.root = root
        self.ttl = ttl
        self.max_bytes = max_bytes
        self._clock = clock

    # -- search results ----------------------------------------------------------

    def _results(self, provider: str, query_key: str) -> Path:
        return self.root / "results" / _key(provider)[:16] / f"{query_key}.json"

    def results(self, provider: str, query_key: str) -> list[CoverCandidate] | None:
        path = self._results(provider, query_key)
        try:
            raw = json.loads(path.read_bytes())
            stored = datetime.fromisoformat(raw["stored_at"])
            if self._clock() - stored > self.ttl:
                return None
            found = [CoverCandidate.model_validate(c) for c in raw["candidates"]]
        except (OSError, ValueError, KeyError, TypeError, ValidationError):
            return None
        self._touch(path)
        return found

    def store_results(
        self, provider: str, query_key: str, candidates: list[CoverCandidate]
    ) -> None:
        body = {
            "stored_at": self._clock().isoformat(),
            "candidates": [c.model_dump(mode="json") for c in candidates],
        }
        _write(self._results(provider, query_key), json.dumps(body).encode())
        for c in candidates:
            self.remember(c)

    # -- candidates by id ----------------------------------------------------------

    def _candidate(self, candidate_id: str) -> Path:
        return self.root / "candidates" / candidate_id[:2] / f"{candidate_id}.json"

    def remember(self, candidate: CoverCandidate) -> None:
        _write(self._candidate(candidate.id), candidate.model_dump_json().encode())

    def candidate(self, candidate_id: str) -> CoverCandidate | None:
        if not candidate_id.isalnum():
            return None
        try:
            return CoverCandidate.model_validate_json(self._candidate(candidate_id).read_bytes())
        except (OSError, ValueError):
            return None

    # -- images ------------------------------------------------------------------------

    def _image(self, url: str) -> Path:
        key = _key(url)
        return self.root / "images" / key[:2] / key

    def image(self, url: str) -> bytes | None:
        path = self._image(url)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        self._touch(path)
        return data

    def store_image(self, url: str, data: bytes) -> None:
        _write(self._image(url), data)
        self.prune()

    # -- size cap ----------------------------------------------------------------------

    @staticmethod
    def _touch(path: Path) -> None:
        with contextlib.suppress(OSError):
            os.utime(path)

    def prune(self) -> int:
        """Remove least recently used files until the cache fits; returns bytes freed."""
        files: list[tuple[float, int, Path]] = []
        total = 0
        if not self.root.is_dir():
            return 0
        for dirpath, _, names in os.walk(self.root):
            for name in names:
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        freed = 0
        if total <= self.max_bytes:
            return 0
        for _, size, p in sorted(files):
            if total - freed <= self.max_bytes:
                break
            try:
                p.unlink()
            except OSError:
                continue
            freed += size
        return freed
