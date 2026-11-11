"""``{{ ... }}`` workflow-context interpolation (WORKFLOW-8 to WORKFLOW-10).

Expressions are dotted paths only (``vars.inbox``, ``steps.import.count``);
there is no evaluation, so a workflow file can't run code through ``{{ }}``.
beets template syntax (``$field``, ``%func{}``) is never touched here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

ROOTS: Final = ("vars", "run", "profile", "steps")
#: Fields of the ``run`` and ``profile`` roots. ``profile`` exposes no secrets.
RUN_FIELDS: Final = ("id", "workflow")
PROFILE_FIELDS: Final = ("name",)

_OPEN = "{{"
_EXPR = re.compile(
    r"\{\{\s*(?:(?P<literal>'\{\{')|(?P<path>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*))\s*\}\}"
)

Scalar = str | int | float | bool


class InterpolationError(ValueError):
    """An expression is malformed or can't be resolved."""


@dataclass(frozen=True)
class Expression:
    path: tuple[str, ...]
    start: int
    end: int

    @property
    def text(self) -> str:
        return "{{ " + ".".join(self.path) + " }}"


@dataclass(frozen=True)
class _Literal:
    start: int
    end: int


def _scan(value: str) -> list[Expression | _Literal]:
    tokens: list[Expression | _Literal] = []
    pos = value.find(_OPEN)
    while pos != -1:
        m = _EXPR.match(value, pos)
        if m is None:
            snippet = value[pos : pos + 30]
            raise InterpolationError(
                f"malformed expression at {snippet!r}: only dotted paths like "
                "'{{ vars.name }}' are allowed; write a literal '{{' as \"{{ '{{' }}\""
            )
        if m.group("literal"):
            tokens.append(_Literal(m.start(), m.end()))
        else:
            tokens.append(Expression(tuple(m.group("path").split(".")), m.start(), m.end()))
        pos = value.find(_OPEN, m.end())
    return tokens


def find_expressions(value: str) -> list[Expression]:
    """All ``{{ path }}`` expressions in ``value``; raises on malformed ``{{``."""
    return [t for t in _scan(value) if isinstance(t, Expression)]


@dataclass(frozen=True)
class InterpolationContext:
    """Values available to ``{{ }}`` when a step starts."""

    vars: Mapping[str, str] = field(default_factory=dict[str, str])
    run: Mapping[str, str] = field(default_factory=dict[str, str])
    profile: Mapping[str, str] = field(default_factory=dict[str, str])
    steps: Mapping[str, Mapping[str, Scalar]] = field(
        default_factory=dict[str, Mapping[str, Scalar]]
    )

    def resolve(self, path: tuple[str, ...]) -> str:
        expr = "{{ " + ".".join(path) + " }}"
        root, *rest = path
        if root == "steps":
            if len(rest) != 2:
                raise InterpolationError(f"{expr}: use steps.<id>.<key>")
            step_id, key = rest
            outputs = self.steps.get(step_id)
            if outputs is None or key not in outputs:
                raise InterpolationError(f"{expr}: step '{step_id}' has no output '{key}'")
            return _render(outputs[key])
        roots: dict[str, Mapping[str, str]] = {
            "vars": self.vars,
            "run": self.run,
            "profile": self.profile,
        }
        if root not in roots:
            raise InterpolationError(f"{expr}: unknown root '{root}' (allowed: {', '.join(ROOTS)})")
        if len(rest) != 1:
            raise InterpolationError(f"{expr}: use {root}.<name>")
        values = roots[root]
        if rest[0] not in values:
            raise InterpolationError(f"{expr}: '{rest[0]}' is not set")
        return values[rest[0]]


def _render(value: Scalar) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def interpolate(value: str, ctx: InterpolationContext) -> str:
    """Replace every ``{{ path }}`` in ``value``; everything else is kept as is."""
    tokens = _scan(value)
    if not tokens:
        return value
    out: list[str] = []
    pos = 0
    for tok in tokens:
        out.append(value[pos : tok.start])
        out.append(_OPEN if isinstance(tok, _Literal) else ctx.resolve(tok.path))
        pos = tok.end
    out.append(value[pos:])
    return "".join(out)
