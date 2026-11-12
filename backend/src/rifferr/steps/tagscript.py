"""The ``tags.script`` step (S-TAGSCRIPT): run a tag script, propose the net changes.

The script runs on each target album with working values (``rifferr.tagscript``);
the differences become one ChangeSet per album through ``ctx.propose``, which
then follows the step's ``apply`` policy. The step runs no user code and needs
no trust (TAGSCRIPT-18).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from rifferr.backends.base import BackendCapabilities, LibraryBackend
from rifferr.core.changeset import (
    ChangeSetProposal,
    ProposedFieldChange,
    ProposedTargetChange,
    TargetRef,
)
from rifferr.core.snapshot import FieldValue
from rifferr.engine import StepContext, StepFailure, StepResult
from rifferr.script_store import ScriptPathError, resolve_script
from rifferr.tagscript.evaluator import (
    IdempotenceCheck,
    IdempotenceIssue,
    ScriptResult,
    check_idempotence,
    run_script,
)
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.models import TagScript, TagScriptParams


@dataclass(frozen=True)
class LoadedScript:
    script: TagScript
    text: str
    path: str | None = None
    sha256: str | None = None


def load(ctx: StepContext, params: TagScriptParams) -> LoadedScript:
    """The step's script: inline, or the file read and interpolated now (TAGSCRIPT-1)."""
    inline = params.inline()
    if inline is not None:
        text = json.dumps(
            inline.model_dump(mode="json", by_alias=True, exclude_none=True), ensure_ascii=False
        )
        return LoadedScript(inline, text)
    assert params.script is not None
    if ctx.profile_dir is None:
        raise StepFailure("this run has no profile folder to read scripts from")
    try:
        path = resolve_script(ctx.profile_dir, params.script)
    except ScriptPathError as exc:
        raise StepFailure(str(exc)) from None
    data = path.read_bytes()
    text = data.decode("utf-8")
    script, errors = parse_script(text, params.script, ctx.interpolate)
    if script is None:
        raise StepFailure("invalid script:\n" + "\n".join(str(e) for e in errors))
    try:
        merged = params.override(script)
    except ValueError as exc:
        raise StepFailure(f"script {params.script}: {exc}") from None
    return LoadedScript(merged, text, params.script, hashlib.sha256(data).hexdigest())


def _show(value: FieldValue) -> str:
    return "(absent)" if value is None else json.dumps(value, ensure_ascii=False)


def issue_text(issue: IdempotenceIssue) -> str:
    who = f"action '{issue.action}'" if issue.action else "a later action"
    return (
        f"{issue.target.id} {issue.field}: a second pass of {who} changes "
        f"{_show(issue.first)} to {_show(issue.second)}"
    )


def proposal(result: ScriptResult, source: str) -> ChangeSetProposal | None:
    """The net changes as one proposal, ``old`` from the snapshot (TAGSCRIPT-14)."""
    targets: list[ProposedTargetChange] = []
    by_target: dict[str, list[ProposedFieldChange]] = {}
    refs: dict[str, TargetRef] = {}
    for d in result.diffs():
        change = ProposedFieldChange(
            field=d.field,
            op="delete" if d.deleted else "set",
            new=None if d.deleted else d.after,
            old=d.before,
            rationale=result.rationale(d) or None,
        )
        by_target.setdefault(d.target.id, []).append(change)
        refs[d.target.id] = d.target
    for target_id, changes in by_target.items():
        targets.append(ProposedTargetChange(target=refs[target_id], changes=changes))
    if not targets:
        return None
    return ChangeSetProposal(rationale=f"tags.script {source}", targets=targets)


class TagScriptStep:
    name = "tags.script"
    params_model = TagScriptParams
    requires = BackendCapabilities(query=True, templates=True, apply=True)

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, TagScriptParams)
        loaded = load(ctx, params)
        script = loaded.script
        source = loaded.path or "(inline)"
        backend: LibraryBackend = ctx.backend
        proposed = changes = warnings = 0
        for album in ctx.albums(scope=script.scope):
            if ctx.cancelled:
                break
            result = run_script(script, album, backend)
            if result.error is not None:
                ctx.fail_target(album.id, str(result.error))
                continue
            check = IdempotenceCheck([])
            if script.idempotence != "off":
                check = check_idempotence(script, result, backend)
            if not check.ok:
                warnings += 1
                stream = "error" if script.idempotence == "fail" else "warning"
                for issue in check.issues:
                    ctx.log(f"{album.id}: not idempotent: {issue_text(issue)}", stream, album.id)
                if check.error is not None:
                    ctx.log(
                        f"{album.id}: not idempotent: the second pass fails: {check.error}",
                        stream,
                        album.id,
                    )
                if script.idempotence == "fail":
                    ctx.fail_target(album.id, "not idempotent; nothing proposed")
                    continue
            p = proposal(result, source)
            start = len(ctx.proposed)
            if p is not None:
                ctx.propose(p)
            made = ctx.proposed[start:]
            n = sum(len(cs.field_changes()) for cs in made)
            proposed += len(made)
            changes += n
            idem = (
                "idempotence off"
                if script.idempotence == "off"
                else ("idempotent" if check.ok else "NOT idempotent")
            )
            what = f"{n} field change{'s' if n != 1 else ''} proposed" if n else "no changes"
            ctx.log(f"{album.id}: {what}; {idem}", target=album.id)
        outputs: dict[str, str | int] = {
            "changesets": proposed,
            "field_changes": changes,
            "not_idempotent": warnings,
            "script_text": loaded.text,
        }
        if loaded.path is not None and loaded.sha256 is not None:
            outputs["script_path"] = loaded.path
            outputs["script_sha256"] = loaded.sha256
        return StepResult(outputs=dict(outputs))
