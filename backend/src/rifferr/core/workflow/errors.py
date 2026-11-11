"""Validation error type shared by loading and validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

PathPart = str | int
Path = tuple[PathPart, ...]


def format_path(path: Sequence[PathPart]) -> str:
    """``("steps", 1, "with", "run")`` -> ``steps[1].with.run``."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            out += f".{part}" if out else part
    return out


@dataclass(frozen=True)
class ValidationError:
    """One problem in a workflow file, with its YAML location (1-based)."""

    source: str
    path: str
    message: str
    line: int | None = None
    column: int | None = None

    def __str__(self) -> str:
        where = self.source
        if self.line is not None:
            where += f":{self.line}"
            if self.column is not None:
                where += f":{self.column}"
        return f"{where}: {self.path}: {self.message}" if self.path else f"{where}: {self.message}"


class WorkflowLoadError(Exception):
    """The file is not valid YAML or doesn't match the workflow schema."""

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        super().__init__("\n".join(str(e) for e in errors))
