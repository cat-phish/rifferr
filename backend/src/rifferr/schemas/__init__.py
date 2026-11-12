"""JSON Schema export for published models.

Add a model to ``PUBLISHED`` when a spec makes it part of a published file
format (workflow files, ChangeSets, snapshots). Never hand-edit ``schemas/``.
"""

from pydantic import BaseModel

from rifferr.core.changeset import ChangeSetProposal
from rifferr.core.snapshot import AlbumSnapshot, ItemSnapshot
from rifferr.tagscript.models import TagScript

#: file stem -> model. The workflow schema is built separately (it depends on
#: the registered step types); see ``export.render``.
PUBLISHED: dict[str, type[BaseModel]] = {
    "album-snapshot": AlbumSnapshot,  # shell step stdin for per: album (S-SHELL)
    "changeset-proposal": ChangeSetProposal,  # ctx.propose; shell emits: changes stdout
    "item-snapshot": ItemSnapshot,  # shell step stdin for per: item
    "tagscript": TagScript,  # <profile>/scripts/*.yaml (S-TAGSCRIPT)
}
