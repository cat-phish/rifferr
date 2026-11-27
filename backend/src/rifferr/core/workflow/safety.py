"""Static safety report for a workflow (SECURITY-11).

Advisory: it lists what a reviewer should look at before trusting a workflow.
The rule lists are data; extend them without touching callers.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Collection, Mapping
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from rifferr.core.workflow.calls import CalledStep, Calls, expand
from rifferr.core.workflow.models import Workflow

NETWORK_TOOLS: frozenset[str] = frozenset(
    {
        "curl",
        "wget",
        "ssh",
        "scp",
        "sftp",
        "rsync",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "telnet",
        "ftp",
    }
)
DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {"rm", "rmdir", "mv", "chmod", "chown", "chgrp", "sudo", "doas", "dd", "shred", "truncate"}
)
SYSTEM_DIRS: tuple[str, ...] = ("/bin/", "/sbin/", "/usr/bin/", "/usr/sbin/", "/usr/local/bin/")
SCRIPT_PREFIXES: tuple[str, ...] = ("./", "../", "~", "/")
INTERPRETERS: frozenset[str] = frozenset(
    {"sh", "bash", "zsh", "dash", "python", "python3", "perl", "ruby", "node", "env"}
)
SEPARATORS: frozenset[str] = frozenset({";", "&&", "||", "|", "&"})
SCRIPT_WARNING = "trust covers the workflow text only, not this script's content"

FindingKind = Literal[
    "command",
    "shell_string",
    "network_tool",
    "destructive",
    "secret",
    "local_script",
    "auto_apply",
    "tag_field",
    "file_move",
    "rerun",
    "ai_request",
    "path_sent",
]
#: beets plugins that copy or move extra files on beets' own move events. ``library.move``
#: moves files itself, so they don't fire (S-LIBRARY-MOVE LIBRARY-MOVE-6a).
ARTIFACT_PLUGINS: frozenset[str] = frozenset({"copyartifacts", "copyartifacts3"})
#: Library subcommands that delete files or library entries (``beets.command``).
DESTRUCTIVE_LIBRARY_COMMANDS: frozenset[str] = frozenset({"remove"})
#: ``beets.import`` duplicate actions that remove or replace an album already in the
#: library (SEC-08).
DESTRUCTIVE_DUPLICATE_ACTIONS: frozenset[str] = frozenset({"remove", "merge", "upgrade"})
VARIABLE_PROGRAM_WARNING = "program comes from a variable: trust does not cover what it runs"


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    kind: FindingKind
    detail: str
    workflow: str | None = None
    """The called file the step comes from (SUBWORKFLOW-14); None for the workflow's own."""


class SafetyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: list[Finding]

    def of_kind(self, kind: FindingKind) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def safety_report(
    wf: Workflow, calls: Calls | None = None, *, beets_plugins: Collection[str] = ()
) -> SafetyReport:
    """``calls``: the files ``wf`` calls; their steps are reported too, by expanded step id
    and file (SUBWORKFLOW-14). ``beets_plugins``: the plugins the profile's beets config
    enables, when known (LIBRARY-MOVE-6a)."""
    if calls is not None:
        wf = expand(wf, calls)
    report = _report(wf, frozenset(beets_plugins))
    files = {s.id: s.workflow for s in wf.steps if isinstance(s, CalledStep)}
    return SafetyReport(
        findings=[
            f.model_copy(update={"workflow": files[f.step_id]}) if f.step_id in files else f
            for f in report.findings
        ]
    )


def _report(wf: Workflow, beets_plugins: frozenset[str] = frozenset()) -> SafetyReport:
    findings: list[Finding] = []
    for name, trigger in (
        ("on.watch", wf.on and wf.on.watch),
        ("on.webhook", wf.on and wf.on.webhook),
    ):
        if trigger is not None and trigger.dedupe == "in_progress":  # TRIGGERS-26
            findings.append(
                Finding(
                    step_id=name,
                    kind="rerun",
                    detail="dedupe: in_progress: an album that reappears in the inbox is "
                    "imported again",
                )
            )
    for step in wf.steps:
        if step.apply == "auto":  # TAGSCRIPT-18; any step that proposes ChangeSets (SEC-65)
            findings.append(
                Finding(
                    step_id=step.id,
                    kind="auto_apply",
                    detail=f"{step.uses} applies its changes without review (apply: auto)",
                )
            )
        findings += _ai_findings(step.id, step.uses, step.with_)  # AI-CORE-20
        if step.uses == "beets.command":
            findings += _library_command_findings(step.id, step.with_)
            continue
        if step.uses == "library.prune":  # LIBRARY-PRUNE-7
            findings.append(
                Finding(
                    step_id=step.id,
                    kind="destructive",
                    detail="removes library entries whose files are missing "
                    "(files are not touched)",
                )
            )
        if step.uses == "files.cleanup":
            findings += _cleanup_findings(step.id, step.with_)
            continue
        if step.uses == "beets.import":
            findings += _import_findings(step.id, step.with_)
        if step.uses == "library.move":
            findings += _move_findings(step.id, step.with_, beets_plugins)
            continue
        if step.uses != "shell":
            continue
        params = step.with_
        run = params.get("run")
        shell = params.get("shell") is True
        if isinstance(run, str):
            findings.append(Finding(step_id=step.id, kind="command", detail=run))
            if shell:
                findings.append(
                    Finding(step_id=step.id, kind="shell_string", detail="runs through /bin/sh -c")
                )
            words = _split(run)
        elif isinstance(run, list):
            words = [str(w) for w in cast(list[Any], run)]
            findings.append(Finding(step_id=step.id, kind="command", detail=shlex.join(words)))
        else:
            words = []
        findings += _word_findings(step.id, words)
        findings += _field_findings(step.id, run, params.get("cwd"))
        secrets = params.get("secrets")
        if isinstance(secrets, list):
            for name in cast(list[Any], secrets):
                findings.append(
                    Finding(step_id=step.id, kind="secret", detail=f"exposes secret '{name}'")
                )
    return SafetyReport(findings=findings)


def _field_findings(step_id: str, run: Any, cwd: Any) -> list[Finding]:
    """SEC-23: tag values in an argv item or ``cwd`` can act as options or leave the folder."""
    out: list[Finding] = []
    if isinstance(run, list):
        items = [str(w) for w in cast(list[Any], run)]
        for i, item in enumerate(items):
            leading = i == 1  # the first argument: nothing before it stops it reading as an option
            if (
                item.startswith(("$", "%"))
                and not item.startswith("$RIFFERR_")
                and leading
                and "--" not in items[:i]
            ):
                out.append(
                    Finding(
                        step_id=step_id,
                        kind="tag_field",
                        detail=(
                            f"argument {item!r} starts with a tag field and could be read as an "
                            "option; put '--' before it"
                        ),
                    )
                )
    if isinstance(cwd, str) and ("$" in cwd or "%" in cwd):
        out.append(
            Finding(
                step_id=step_id,
                kind="tag_field",
                detail=f"cwd {cwd!r} contains a tag field: a tag value can point it elsewhere",
            )
        )
    return out


def _cleanup_findings(step_id: str, params: dict[str, Any]) -> list[Finding]:
    """A ``files.cleanup`` step in delete mode removes files (FILES-CLEANUP-13, -17)."""
    if params.get("mode") != "delete":
        return []
    patterns = params.get("delete")
    names = [str(p) for p in cast(list[Any], patterns)] if isinstance(patterns, list) else []
    parts = [f"deletes in {params.get('path', '?')}: {', '.join(names) or 'empty folders only'}"]
    trash = params.get("trash")
    if isinstance(trash, str):
        parts.append(f"moved to trash {trash}")
    if params.get("review") is True:
        parts.append("asks in the review queue first")
    if params.get("allow_audio") is True:
        parts.append("allow_audio: may delete music the library could import")
    return [Finding(step_id=step_id, kind="destructive", detail="; ".join(parts))]


def _import_findings(step_id: str, params: dict[str, Any]) -> list[Finding]:
    """A ``beets.import`` step's options that move files or replace library albums
    outside ChangeSets (SEC-65). Options left to the user's beets config are not listed."""
    raw = params.get("options")
    options = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    path = params.get("path", "?")
    out: list[Finding] = []
    if options.get("move") is True:
        out.append(
            Finding(
                step_id=step_id,
                kind="destructive",
                detail=f"moves the files in {path} into the library (move: true)",
            )
        )
    if options.get("write") is True:
        out.append(
            Finding(
                step_id=step_id, kind="destructive", detail="writes tags into the imported files"
            )
        )
    action = options.get("duplicate_action")
    if action in DESTRUCTIVE_DUPLICATE_ACTIONS:
        out.append(
            Finding(
                step_id=step_id,
                kind="destructive",
                detail=f"duplicate_action: {action}: may remove an album already in the "
                "library; needs a trusted workflow",
            )
        )
    return out


def _artifact_rule(rule: Any) -> str:
    if not isinstance(rule, dict):
        return str(rule)
    r = cast(dict[str, Any], rule)
    match = r.get("match")
    globs = (
        " ".join(str(m) for m in cast(list[Any], match)) if isinstance(match, list) else str(match)
    )
    return f"{globs} -> {r['to']}/" if r.get("to") else globs


def _move_findings(
    step_id: str, params: dict[str, Any], beets_plugins: frozenset[str]
) -> list[Finding]:
    """A ``library.move`` step moves or copies album files (LIBRARY-MOVE-6a). It is
    journaled and undoable, so it is listed, not flagged as destructive."""
    verb = "copies" if params.get("mode") == "copy" else "moves"
    where = params.get("directory") or "beets' directory"
    rules = params.get("artifacts", ["*"])
    names = [_artifact_rule(r) for r in (cast(list[Any], rules) if isinstance(rules, list) else [])]
    travel = ", ".join(names) or "none"
    detail = f"{verb} album files into {where}; other files that travel: {travel}"
    out = [Finding(step_id=step_id, kind="file_move", detail=detail)]
    for plugin in sorted(ARTIFACT_PLUGINS & beets_plugins):
        out.append(
            Finding(
                step_id=step_id,
                kind="file_move",
                detail=f"the profile's beets config enables {plugin}, which this step "
                "bypasses: it moves files itself, so beets' move events don't fire; its "
                "artifacts setting decides which other files travel",
            )
        )
    return out


def _library_command_findings(step_id: str, params: dict[str, Any]) -> list[Finding]:
    """A ``beets.command`` step changes the library outside ChangeSets (BEETS-COMMAND-13)."""
    command = str(params.get("command", ""))
    args = params.get("args")
    words = [str(a) for a in cast(list[Any], args)] if isinstance(args, list) else []
    out = [Finding(step_id=step_id, kind="command", detail=shlex.join(["beet", command, *words]))]
    if command in DESTRUCTIVE_LIBRARY_COMMANDS:
        out.append(Finding(step_id=step_id, kind="destructive", detail=f"beet {command}"))
    return out


def _split(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


def _program_positions(words: list[str]) -> set[int]:
    """Indexes of words that are programs: the first word, the first after a shell
    separator, and the script an interpreter runs."""
    positions: set[int] = set()
    expect = True
    for i, word in enumerate(words):
        if word in SEPARATORS:
            expect = True
            continue
        if expect:
            positions.add(i)
            expect = os.path.basename(word) in INTERPRETERS
            if expect and i + 1 < len(words) and words[i + 1].startswith("-"):
                expect = False  # e.g. sh -c '...', python -m mod
    return positions


def _word_findings(
    step_id: str, words: list[str], seen: set[tuple[str, str]] | None = None
) -> list[Finding]:
    out: list[Finding] = []
    seen = set[tuple[str, str]]() if seen is None else seen
    programs = _program_positions(words)
    for index, word in enumerate(words):
        templated = "$" in word or "{{" in word
        if index in programs and templated and ("local_script", word) not in seen:
            seen.add(("local_script", word))
            out.append(
                Finding(
                    step_id=step_id,
                    kind="local_script",
                    detail=f"{word}: {VARIABLE_PROGRAM_WARNING}",
                )
            )
        if (
            index in programs
            and os.path.basename(word) in INTERPRETERS
            and index + 2 < len(words)
            and words[index + 1] == "-c"
        ):
            # SEC-65: the code an interpreter gets as a string is scanned too
            if ("shell_string", word) not in seen:
                seen.add(("shell_string", word))
                out.append(
                    Finding(
                        step_id=step_id,
                        kind="shell_string",
                        detail=f"runs code given as a string: {os.path.basename(word)} -c",
                    )
                )
            out += _word_findings(step_id, _split(words[index + 2]), seen)
        name = os.path.basename(word)
        for kind, tools in (("network_tool", NETWORK_TOOLS), ("destructive", DESTRUCTIVE_TOOLS)):
            if name in tools and (kind, name) not in seen:
                seen.add((kind, name))
                out.append(Finding(step_id=step_id, kind=kind, detail=name))  # type: ignore[arg-type]
        if index in programs and _is_local_script(word) and ("local_script", word) not in seen:
            seen.add(("local_script", word))
            out.append(
                Finding(step_id=step_id, kind="local_script", detail=f"{word}: {SCRIPT_WARNING}")
            )
    return out


def _is_local_script(word: str) -> bool:
    if "$" in word or "{{" in word:
        return False  # a template or variable, not a fixed path
    if not word.startswith(SCRIPT_PREFIXES):
        return False
    return not word.startswith(SYSTEM_DIRS)


#: Fields whose values are file paths: sending them reveals the folder layout (AI-CORE-20).
PATH_FIELDS: frozenset[str] = frozenset({"path", "artpath"})


def _ai_findings(step_id: str, uses: str, params: Mapping[str, Any]) -> list[Finding]:
    """An ``ai_request`` finding for a step that runs an AI task (AI-CORE-20): ``ai.tag``,
    or any step with an ``ai`` block (e.g. ``tags.choose``, F-AI-SUGGEST)."""
    block: Mapping[str, Any] | None
    if uses == "ai.tag":
        block = params
    else:
        raw = params.get("ai")
        block = cast("Mapping[str, Any]", raw) if isinstance(raw, Mapping) else None
    if block is None:
        return []
    sent: list[str] = []
    for key in ("context", "item_context"):
        value = block.get(key)
        if isinstance(value, list):
            sent += [str(v) for v in cast("list[Any]", value)]
    provider = block.get("provider")
    where = (
        f"provider '{provider}'" if isinstance(provider, str) else "the profile's default provider"
    )
    fields = ", ".join(sent) if sent else "no album fields"
    findings = [
        Finding(
            step_id=step_id,
            kind="ai_request",
            detail=f"sends album data ({fields}) to {where}; it leaves this machine unless "
            "the provider's host is local",
        )
    ]
    for name in sorted(PATH_FIELDS & set(sent)):
        findings.append(
            Finding(
                step_id=step_id,
                kind="path_sent",
                detail=f"sends the file path field '{name}' to the AI provider",
            )
        )
    return findings
