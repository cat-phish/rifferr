"""Import-boundary checker used by test_boundaries.py (AGENTS.md "Layout")."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: Layers that may use only the backend protocol, never an implementation.
BACKEND_CLIENT_LAYERS = ("core", "engine", "tagscript", "steps", "covers", "ai")
#: Layers that must stay pure: no beets, network or process access.
PURE_LAYERS = ("core", "engine", "tagscript")
IO_MODULES = (
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib.request",
    "aiohttp",
    "asyncio.subprocess",
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    module: str
    rule: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: imports {self.module} ({self.rule})"


def _imports(tree: ast.AST, own_module: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # resolve relative imports against the importing module
                base = own_module.split(".")[: -node.level]
                mod = ".".join([*base, node.module] if node.module else base)
            else:
                mod = node.module or ""
            found.append((node.lineno, mod))
            found += [(node.lineno, f"{mod}.{a.name}") for a in node.names]
    return found


def _matches(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _rules_broken(mod: str, layer: str, in_beets_backend: bool) -> list[str]:
    broken: list[str] = []
    if _matches(mod, "beets") and not in_beets_backend:
        broken.append("only rifferr.backends.beets may import beets")
    if (
        layer in BACKEND_CLIENT_LAYERS
        and _matches(mod, "rifferr.backends")
        and mod != "rifferr.backends"
        and not _matches(mod, "rifferr.backends.base")
    ):
        broken.append(f"rifferr.{layer} may import only rifferr.backends.base")
    if layer in PURE_LAYERS and any(_matches(mod, io) for io in IO_MODULES):
        broken.append(f"rifferr.{layer} is pure: no network or process modules")
    return broken


def check(package_root: Path) -> list[Violation]:
    """Check every module under ``package_root`` (the ``rifferr`` package dir)."""
    violations: list[Violation] = []
    for path in sorted(package_root.rglob("*.py")):
        rel = path.relative_to(package_root)
        parts = rel.with_suffix("").parts
        parts = parts[:-1] if parts[-1] == "__init__" else parts
        own = ".".join(("rifferr", *parts))
        layer = parts[0] if parts else ""
        in_beets_backend = _matches(own, "rifferr.backends.beets")
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        rules: dict[tuple[int, str], str] = {}
        for line, mod in _imports(tree, own):
            for rule in _rules_broken(mod, layer, in_beets_backend):
                rules.setdefault((line, rule), mod)
        violations += [Violation(str(rel), line, mod, rule) for (line, rule), mod in rules.items()]
    return violations
