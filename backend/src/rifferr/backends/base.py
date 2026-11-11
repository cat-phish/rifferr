"""LibraryBackend protocol and capabilities.

C-BACKEND fills in the protocol and snapshots. ``BackendCapabilities`` lands
first because workflow validation (WORKFLOW-6) checks step requirements
against it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class BackendCapabilities(BaseModel):
    """What a library backend can do (BACKEND-2).

    Step types declare the capabilities they need with the same model: a field
    set to ``True`` means "required".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    import_: bool = False
    query: bool = False
    templates: bool = False
    apply: bool = False
    undo: bool = False
    command_env: bool = False

    @staticmethod
    def display_name(field: str) -> str:
        return field.rstrip("_")

    def missing(self, required: BackendCapabilities) -> list[str]:
        """Display names of capabilities ``required`` asks for that ``self`` lacks."""
        return [
            self.display_name(name)
            for name in type(self).model_fields
            if getattr(required, name) and not getattr(self, name)
        ]
