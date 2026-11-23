"""The ``covers.resize`` step (S-COVERS-RESIZE).

Once per album: when its cover is larger than ``max`` pixels on a side, keep the
original next to it under the ``original`` name and make the cover a smaller copy. Both
happen in one ChangeSet: the kept original is a file copy (from the art store, so undo
can check it), then the album's ``art_sha256`` changes to the resized image. Journaled,
reviewable and undoable like any FileChangeSet.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from rifferr.artstore import ArtStore
from rifferr.backends.base import BackendCapabilities
from rifferr.core.changeset import (
    ChangeSetProposal,
    FileEffect,
    ProposedFieldChange,
    TargetRef,
)
from rifferr.core.covers import ART_FIELD, sniff_image
from rifferr.core.moves import MovePlan
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.covers.resize import ImageResizer, OutputFormat, ResizeError, load_resizers
from rifferr.engine.steps import FileExtras, StepContext, StepFailure, StepResult
from rifferr.steps.covers_search import album_label

ART_PATH_FIELD: Final = "artpath"
"""The snapshot field naming the album's art file (as the art endpoint reads it)."""


class CoversResizeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max: int = Field(ge=1, le=20000, description="Longest side in pixels; larger covers shrink")
    original: str = Field(
        default="{stem}-hires{ext}",
        min_length=1,
        description="Name the original is kept under, next to the cover; {stem} and {ext} "
        "are the cover file's name without and with only its extension",
    )
    format: OutputFormat = Field(
        default="keep", description="keep: the cover's own format; jpeg: always JPEG"
    )
    quality: int = Field(default=90, ge=1, le=95, description="JPEG quality")
    resizer: str = Field(default="pillow", min_length=1, description="Image resizer to use")

    @field_validator("original")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in (".", ".."):
            raise ValueError("must be a file name, not a path")
        if "{stem}" not in value and "{ext}" not in value:
            raise ValueError("must contain {stem} or {ext}, so it differs from the cover")
        return value

    def kept_name(self, art_name: str) -> str:
        stem, ext = posixpath.splitext(art_name)
        return self.original.replace("{stem}", stem).replace("{ext}", ext)


class CoversResizeStep:
    name = "covers.resize"
    waits = "apply"  # only with apply: review (UI-REFRESH-13)
    params_model = CoversResizeParams
    requires = BackendCapabilities(query=True, apply=True, art=True, move=True)
    default_apply = "auto"  # COVERS-RESIZE-6

    def __init__(
        self,
        resizers: Mapping[str, ImageResizer] | None = None,
        art_root: Path | None = None,
    ) -> None:
        self._resizers = dict(resizers) if resizers is not None else None
        self._art_root = art_root

    def resizers(self) -> dict[str, ImageResizer]:
        if self._resizers is None:
            self._resizers, _ = load_resizers()
        return self._resizers

    async def run(self, ctx: StepContext) -> StepResult:
        params = ctx.params
        assert isinstance(params, CoversResizeParams)
        resizer = self.resizers().get(params.resizer)
        if resizer is None:
            raise StepFailure(f"no image resizer named {params.resizer}")
        from rifferr.profile_store import art_store_path

        store = ArtStore(self._art_root or art_store_path(ctx.profile))
        counts = {"resized": 0, "skipped": 0, "failed": 0}
        for album in ctx.albums():
            if ctx.cancelled:
                break
            outcome = self._album(ctx, album, params, resizer, store)
            counts[outcome] += 1
        return StepResult(outputs=dict(counts))

    def _album(
        self,
        ctx: StepContext,
        album: AlbumSnapshot,
        params: CoversResizeParams,
        resizer: ImageResizer,
        store: ArtStore,
    ) -> str:
        label = album_label(album)
        sha, path = album.fields.get(ART_FIELD), album.fields.get(ART_PATH_FIELD)
        if not isinstance(sha, str) or not isinstance(path, str):  # COVERS-RESIZE-2
            ctx.log(f"{label}: no cover, skipped", target=album.id)
            return "skipped"
        data = self._read(store, sha, path)
        if data is None:
            ctx.fail_target(album.id, f"{label}: can't read the cover {path}")
            return "failed"
        info = sniff_image(data)
        if info is None:
            ctx.log(f"{label}: cover isn't a JPEG or PNG, skipped", "warning", album.id)
            return "skipped"
        if max(info.width, info.height) <= params.max:
            ctx.log(f"{label}: cover {info.width}x{info.height}, small enough", target=album.id)
            return "skipped"
        try:
            smaller = resizer.resize(data, params.max, params.format, params.quality)
        except ResizeError as exc:
            ctx.fail_target(album.id, f"{label}: {exc}")
            return "failed"
        new_info = sniff_image(smaller)
        new_sha, _ = store.put(smaller)
        _, original = store.put(data)  # the copy's source: undo checks it (COVERS-RESIZE-5)
        folder = posixpath.dirname(path)
        kept_name = params.kept_name(posixpath.basename(path))
        kept = posixpath.join(folder, kept_name)
        size = f"{new_info.width}x{new_info.height}" if new_info else "?"
        rationale = f"cover {info.width}x{info.height} -> {size}; original kept as {kept_name}"
        change = ProposedFieldChange(field=ART_FIELD, old=sha, new=new_sha)
        ref = TargetRef(scope="album", id=album.id)
        existing = ctx.backend.file_sha256(kept)
        try:
            if existing is None:
                effect = FileEffect(
                    target=ref,
                    kind="artifact",  # not the library's art: its path isn't stored
                    src=str(original),
                    dst=kept,
                    mode="copy",
                    size=len(data),
                    sha256=sha,
                )
                plan = MovePlan(
                    album=ref, label=label, src_dir=folder, dst_dir=folder, files=[effect]
                )
                ctx.propose_files(plan, extras=FileExtras(changes=(change,), rationale=rationale))
            elif existing == sha:  # COVERS-RESIZE-4: already kept, only the cover changes
                ctx.propose(ChangeSetProposal(changes=[change], rationale=rationale), ref)
            else:
                ctx.fail_target(
                    album.id, f"{label}: {kept} exists with other content; not overwritten"
                )
                return "failed"
        except StepFailure as exc:
            ctx.fail_target(album.id, f"{label}: {exc}")
            return "failed"
        ctx.log(f"{label}: {rationale}", target=album.id)
        return "resized"

    @staticmethod
    def _read(store: ArtStore, sha: str, path: str) -> bytes | None:
        found = store.find(sha)
        try:
            return (found or Path(path)).read_bytes()
        except OSError:
            return None
