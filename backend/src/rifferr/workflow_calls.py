"""Read the workflow files a workflow calls (S-SUBWORKFLOW), for the server and CLI.

Called files live in the profile's ``workflows/`` folder. They are read as bytes and
decoded strictly as UTF-8, like the caller (``rifferr.trust``), so each stored text
hashes back to the bytes that were trusted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rifferr.appdb import AppDB
from rifferr.core.workflow import Calls, RoundTripDoc, Workflow, resolve_calls, stored_reader
from rifferr.core.workflow.calls import ReadFile
from rifferr.trust import WorkflowFileError, read_workflow_file, workflow_hash


def reader(workflows_dir: Path) -> ReadFile:
    root = workflows_dir.resolve()

    def read(path: str) -> str:
        target = (workflows_dir / path).resolve()
        if not target.is_relative_to(root):  # a symbolic link out of the folder
            raise ValueError(f"workflow '{path}' leaves the profile's workflows/ folder")
        if not target.is_file():
            raise FileNotFoundError(path)
        try:
            text, _ = read_workflow_file(target)
        except WorkflowFileError as exc:
            raise ValueError(str(exc)) from None
        return text

    return read


def read_calls(
    workflows_dir: Path, wf: Workflow, doc: RoundTripDoc | None, caller: Path | None = None
) -> Calls:
    """Every file ``wf`` calls, read now. ``caller``: ``wf``'s own file, so that a call
    back to it is a cycle."""
    name: str | None = None
    if caller is not None:
        try:
            name = caller.resolve().relative_to(workflows_dir.resolve()).as_posix()
        except ValueError:
            name = None
    return resolve_calls(wf, doc, reader(workflows_dir), name=name)


@dataclass(frozen=True)
class CalledTrust:
    path: str
    sha256: str
    trusted: bool
    text: str


def called_trust(db: AppDB, calls: Calls | None, profile: str | None) -> list[CalledTrust]:
    """Each readable called file with its hash and whether it is trusted in ``profile``
    (SUBWORKFLOW-13, SEC-63; ``None``: any trust row, for the CLI without a profile)."""
    if calls is None:
        return []
    out: list[CalledTrust] = []
    for path, f in sorted(calls.files.items()):
        if not f.text and f.workflow is None:
            continue  # unreadable; validation reports it
        sha = workflow_hash(f.text)
        out.append(CalledTrust(path, sha, db.is_trusted(sha, profile), f.text))
    return out


def stored_calls(wf: Workflow, doc: RoundTripDoc | None, payload: Mapping[str, Any]) -> Calls:
    """The calls of a run's workflow from the texts stored with its job (SUBWORKFLOW-12)."""
    texts: dict[str, str] = {}
    called = payload.get("called")
    if isinstance(called, list):
        for entry in cast(list[Any], called):
            if isinstance(entry, dict):
                e = cast(dict[str, Any], entry)
                texts[str(e.get("path"))] = str(e.get("source", ""))
    return resolve_calls(wf, doc, stored_reader(texts))


def called_hashes(payload: Mapping[str, Any]) -> dict[str, str]:
    """Called file -> SHA-256, as stored with a run's job (SUBWORKFLOW-15)."""
    called = payload.get("called")
    if not isinstance(called, list):
        return {}
    return {
        str(e.get("path")): str(e.get("sha256"))
        for e in cast(list[dict[str, Any]], called)
        if isinstance(e, dict)
    }
