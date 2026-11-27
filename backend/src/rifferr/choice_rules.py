"""Rules files of ``tags.choose`` steps (S-TAGS-CHOOSE TAGS-CHOOSE-8 to -10).

<profile>/rules/<name>.yaml

A rules file lists options as data: each rule has a label, a backend query (a beets
query) and tag-script actions. ``fallback`` turns values no rule matches into options
through a template. ``learn`` appends rules, round-tripped with ruamel.yaml so comments
and order survive.
"""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from rifferr.core.changeset import FieldName
from rifferr.core.snapshot import FieldValue
from rifferr.profile_store import atomic_write
from rifferr.tagscript.models import Action, SetAction, script_path_problem

RULES_DIR = "rules"
#: ``{{ value }}`` (a fallback template) and ``{{ choice }}`` (fallback actions).
PLACEHOLDER = re.compile(r"\{\{\s*(value|choice)\s*\}\}")


class RulesError(ValueError):
    """The rules file can't be found, read or used."""


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    when: str | None = Field(
        default=None,
        min_length=1,
        description="Backend query (a beets query). Without it the rule is a known value: "
        "suggested while typing a custom value, never ranked or offered (TAGS-CHOOSE-8)",
    )
    actions: Annotated[list[Action], Field(min_length=1)]
    description: str | None = Field(
        default=None,
        max_length=500,
        description="What this label means; shown in review and sent to the AI (AI-SUGGEST-3)",
    )


class Fallback(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    template: str = Field(min_length=1, description="Beets template; {{ value }} is the value")
    actions: Annotated[list[dict[str, Any]], Field(min_length=1)] = Field(
        description="Tag-script actions; {{ choice }} is the template's result"
    )


class RulesFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: FieldName | None = Field(default=None, description="What the actions set")
    rules: list[Rule] = Field(default_factory=list[Rule])
    fallback: Fallback | None = None


def rules_dir(profile_dir: Path) -> Path:
    return profile_dir / RULES_DIR


def resolve_rules(profile_dir: Path, path: str) -> Path:
    """The file ``path`` names under ``rules/``; one that leaves the folder, lexically or
    through a symlink, is an error (S-TAGS-CHOOSE Security & safety)."""
    problem = script_path_problem(path)
    if problem:
        raise RulesError(f"rules file {path!r}: {problem.replace('scripts/', 'rules/')}")
    base = rules_dir(profile_dir)
    real_base = Path(os.path.realpath(base))
    real = Path(os.path.realpath(base / path))
    if not real.is_relative_to(real_base):
        raise RulesError(f"rules file {path!r} leaves the profile's rules/ folder")
    if not real.is_file():
        raise RulesError(f"rules file {path!r} not found in {base}")
    return real


def load_rules(path: Path) -> RulesFile:
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
        return RulesFile.model_validate(data or {})
    except (OSError, YAMLError) as exc:
        raise RulesError(f"rules file {path.name}: {exc}") from None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise RulesError(f"rules file {path.name}: {problems}") from None


def substitute(value: Any, name: str, text: str) -> Any:
    """``{{ name }}`` replaced by ``text`` in every string of ``value``."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: text if m.group(1) == name else m.group(0), value)
    if isinstance(value, list):
        return [substitute(v, name, text) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: substitute(v, name, text) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def rule_values(rules: RulesFile) -> list[str]:
    """Every value the rules' ``set`` actions give the target field, in file order, each
    once: the known values a custom entry suggests (TAGS-CHOOSE-4)."""
    out: list[str] = []
    if rules.target is None:
        return out
    for rule in rules.rules:
        for action in rule.actions:
            if not isinstance(action, SetAction) or rules.target not in action.set:
                continue
            value = action.set[rules.target]
            for v in value if isinstance(value, list) else [value]:
                text = str(v).strip()
                if text and text not in out:
                    out.append(text)
    return out


def query_literal(value: str) -> str:
    """A regex that matches exactly ``value``, for a learned rule's query."""
    return "^" + re.escape(value).replace("\\ ", " ") + "$"


def append_rule(path: Path, label: str, when: str | None, target: str, value: FieldValue) -> str:
    """Append a rule to the file, keeping comments and order (TAGS-CHOOSE-10). Returns a
    line saying what was added."""
    yaml = YAML()
    yaml.preserve_quotes = True
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise RulesError(f"rules file {path.name}: {exc}") from None
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, CommentedMap):
        raise RulesError(f"rules file {path.name} is not a mapping")
    rules = doc.get("rules")
    if rules is None:
        rules = CommentedSeq()
        doc["rules"] = rules
    if not isinstance(rules, CommentedSeq):
        raise RulesError(f"rules file {path.name}: rules is not a list")
    rule = CommentedMap()
    rule["label"] = label
    if when is not None:
        rule["when"] = when
    setter = CommentedMap()
    setter[target] = list(value) if isinstance(value, list) else value
    action = CommentedMap()
    action["set"] = setter
    rule["actions"] = CommentedSeq([action])
    rules.append(rule)
    out = io.StringIO()
    yaml.dump(doc, out)
    atomic_write(path, out.getvalue().encode("utf-8"))
    if when is None:
        return f"{path.name}: added {label!r} to the known values"
    return f"{path.name}: added rule {label!r} for {when!r}"
