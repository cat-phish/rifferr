"""Static safety report for a workflow (SECURITY-11).

Advisory: it lists what a reviewer should look at before trusting a workflow.
The rule lists are data; extend them without touching callers.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict

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
]
#: Step types that only propose ChangeSets; with ``apply: auto`` they change tags
#: without review, which the report lists as information (TAGSCRIPT-18).
PROPOSING_STEPS: frozenset[str] = frozenset({"tags.script"})


class Finding(BaseModel):
    model_config = ConfigDict(frozen=True)

    step_id: str
    kind: FindingKind
    detail: str


class SafetyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: list[Finding]

    def of_kind(self, kind: FindingKind) -> list[Finding]:
        return [f for f in self.findings if f.kind == kind]


def safety_report(wf: Workflow) -> SafetyReport:
    findings: list[Finding] = []
    for step in wf.steps:
        if step.uses in PROPOSING_STEPS and step.apply == "auto":
            findings.append(
                Finding(
                    step_id=step.id,
                    kind="auto_apply",
                    detail=f"{step.uses} applies its changes without review (apply: auto)",
                )
            )
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
        secrets = params.get("secrets")
        if isinstance(secrets, list):
            for name in cast(list[Any], secrets):
                findings.append(
                    Finding(step_id=step.id, kind="secret", detail=f"exposes secret '{name}'")
                )
    return SafetyReport(findings=findings)


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


def _word_findings(step_id: str, words: list[str]) -> list[Finding]:
    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    programs = _program_positions(words)
    for index, word in enumerate(words):
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
