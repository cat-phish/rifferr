"""JSON Schema export for published models.

Add a model to ``PUBLISHED`` when a spec makes it part of a published file
format (workflow files, ChangeSets, snapshots). Never hand-edit ``schemas/``.
"""

from pydantic import BaseModel

#: file stem -> model, e.g. {"workflow": Workflow}. Filled in by later specs.
PUBLISHED: dict[str, type[BaseModel]] = {}
