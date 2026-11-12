"""Workflow trust by content hash (F-SECURITY, SECURITY-10, -12).

The hash is SHA-256 of the file's bytes. Workflow files are read as bytes and
decoded strictly as UTF-8 (no newline translation), so the text the worker runs
encodes back to exactly the hashed bytes.
"""

from __future__ import annotations

import hashlib
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


def trust_file(db: AppDB, path: Path, trusted_by: str) -> str:
    """Trust the current content of ``path``. Only for an explicit user action."""
    _, sha = read_workflow_file(path)
    db.trust(sha, str(path), trusted_by)
    return sha
