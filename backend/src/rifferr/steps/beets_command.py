"""The ``beets.command`` step (S-BEETS-COMMAND): run a ``beet`` subcommand on the run's
albums, as a subprocess, exactly as in a terminal.

The backend writes the command line (BACKEND-14), so query syntax stays with it.
The subcommand changes the library itself, bypassing ChangeSets: this is the one
exception to "all modifications are ChangeSets" (AGENTS.md non-negotiable 3), so the
step runs only in trusted workflows and logs what changed afterwards.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rifferr.backends.base import BackendCapabilities
from rifferr.core.duration import Duration, parse_duration
from rifferr.core.snapshot import AlbumSnapshot, FieldValue, ItemSnapshot, label
from rifferr.engine import StepContext, StepFailure, StepRefused, StepResult
from rifferr.steps.process import run_process
from rifferr.steps.shell import inherited_env

SUBCOMMAND_PATTERN: Final = r"^[a-z][a-z0-9_-]*$"

#: Subcommands the step refuses, with the reason (BEETS-COMMAND-3).
REFUSED: Final = {
    "import": "use the beets.import step to import",
    "config": "beet config can open an editor; it can't run unattended",
    "edit": "beet edit opens an editor; it can't run unattended",
}


class BeetsCommandParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(
        pattern=SUBCOMMAND_PATTERN,
        description="One beet subcommand, e.g. move, replaygain, fetchart, mbsync "
        "(https://beets.readthedocs.io/en/stable/reference/cli.html). Options go in args.",
    )
    args: list[str] = Field(
        default_factory=list[str],
        description="Options and arguments for the subcommand, e.g. [-a]. beets templates "
        "($path, %if{}) reach beets unchanged. The run's albums are added as the query.",
    )
    timeout: Duration = Field(default="1h", description="Stop the command after this long")

    @field_validator("command", mode="before")
    @classmethod
    def _one_subcommand(cls, value: object) -> object:
        if isinstance(value, str) and (" " in value or value != value.strip()):
            raise ValueError("give one subcommand name; options go in args, e.g. args: [-a]")
        if isinstance(value, str) and value in REFUSED:
            raise ValueError(f"'{value}' is not allowed here: {REFUSED[value]}")
        return value


class BeetsCommandStep:
    name = "beets.command"
    params_model = BeetsCommandParams
    requires = BackendCapabilities(query=True, cli=True)

    def __init__(self, environ: Mapping[str, str] | None = None) -> None:
        self._environ = environ

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, BeetsCommandParams)
        if not ctx.trusted:
            raise StepRefused(
                "this workflow is not trusted, so beets.command steps can't run; "
                "trust it before running it (F-SECURITY)"
            )
        if sys.platform == "win32":
            raise StepRefused("beets.command needs Linux or macOS; on Windows use the Docker image")
        if ctx.selection.run_id is None:  # never the whole library (BEETS-COMMAND-4)
            raise StepFailure("beets.command needs a run to select albums from")
        before = ctx.albums()
        if not before:
            ctx.log("nothing to do")
            return StepResult(outputs={"albums": 0, "changed": 0})
        try:
            argv = ctx.backend.cli_command(params.command, list(params.args), ctx.selection)
        except ValueError as exc:
            raise StepFailure(str(exc)) from exc
        environ = os.environ if self._environ is None else self._environ
        env = {
            **inherited_env(ctx, environ),
            **(ctx.backend.command_env() if ctx.backend.capabilities.command_env else {}),
            "RIFFERR_RUN_ID": ctx.run_id,
            "RIFFERR_PROFILE": ctx.profile,
        }
        problem = await run_process(
            ctx,
            argv,
            cwd=str(ctx.profile_dir or ctx.work_dir) if ctx.profile_dir or ctx.work_dir else None,
            env=env,
            timeout=parse_duration(params.timeout).total_seconds(),
            timeout_text=params.timeout,
        )
        if ctx.cancelled:
            return StepResult()
        if problem is not None:
            raise StepFailure(f"beet {params.command} {problem}")
        changed = _log_changes(ctx, before, ctx.albums())
        return StepResult(outputs={"albums": len(before), "changed": changed})


def _log_changes(ctx: StepContext, before: list[AlbumSnapshot], after: list[AlbumSnapshot]) -> int:
    """One line per album: the fields the command changed (BEETS-COMMAND-12)."""
    now = {a.id: a for a in after}
    changed = 0
    for old in before:
        new = now.get(old.id)
        if new is None:
            ctx.log(f"{label(old)}: no longer selected (removed from the library?)", target=old.id)
            changed += 1
            continue
        fields = _changed_fields(old, new)
        if fields:
            changed += 1
            ctx.log(f"{label(new)}: {', '.join(fields)} changed", target=new.id)
        else:
            ctx.log(f"{label(new)}: no field changes", target=new.id)
    return changed


def _changed_fields(old: AlbumSnapshot, new: AlbumSnapshot) -> list[str]:
    """Field names that differ on the album or any of its items, plus ``path``."""
    names: set[str] = set()
    names |= _diff(old.fields, new.fields)
    if old.path != new.path:
        names.add("path")
    items = {i.id: i for i in new.items}
    for item in old.items:
        other = items.get(item.id)
        if other is None:
            names.add("items")
            continue
        names |= {f"item {n}" for n in _item_diff(item, other)}
    if set(items) - {i.id for i in old.items}:
        names.add("items")
    # An album field copied to its items shows once, as the album field.
    names -= {f"item {n}" for n in names if not n.startswith("item ")}
    return sorted(names)


def _item_diff(old: ItemSnapshot, new: ItemSnapshot) -> set[str]:
    names = _diff(old.fields, new.fields)
    if old.path != new.path:
        names.add("path")
    return names


#: Bookkeeping that changes whenever beets touches a file; not worth a log line.
IGNORED_FIELDS: Final = frozenset({"mtime"})


def _diff(old: Mapping[str, FieldValue], new: Mapping[str, FieldValue]) -> set[str]:
    keys = (set(old) | set(new)) - IGNORED_FIELDS
    return {k for k in keys if (k in old) != (k in new) or old.get(k) != new.get(k)}
