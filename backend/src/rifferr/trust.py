"""Workflow trust by content hash (F-SECURITY, SECURITY-10, -12).

The hash is SHA-256 of the file's bytes. Workflow files are read as bytes and
decoded strictly as UTF-8 (no newline translation), so the text the worker runs
encodes back to exactly the hashed bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from rifferr.appdb import AppDB


class WorkflowFileError(Exception):
    pass


def workflow_hash(source: str | bytes) -> str:
    data = source.encode("utf-8") if isinstance(source, str) else source
    return hashlib.sha256(data).hexdigest()


def read_workflow_file(path: Path) -> tuple[str, str]:
    """Return ``(text, sha256)`` for a workflow file."""
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowFileError(f"{path} is not UTF-8 text: {exc.reason}") from None
    return text, workflow_hash(data)


def trust_file(db: AppDB, path: Path, trusted_by: str, profile: str | None = None) -> str:
    """Trust the current content of ``path`` in ``profile`` (``None``: every profile).
    Only for an explicit user action."""
    _, sha = read_workflow_file(path)
    db.trust(sha, str(path), trusted_by, profile)
    return sha


def config_trust_checker(db_path: Path, profile: str) -> Callable[[str], bool]:
    """SEC-62: ``sha256 -> trusted`` for a profile's beets config. Opens the app DB per
    call, so a worker needs no open handle at start-up."""

    def check(sha256: str) -> bool:
        db = AppDB(db_path)
        try:
            return db.is_trusted(sha256, profile)
        finally:
            db.close()

    return check
