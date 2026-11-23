"""Cover resizing for ``covers.resize`` (S-COVERS-RESIZE).

An ``ImageResizer`` turns image bytes into smaller image bytes. The built-in one uses
Pillow; plugins register others in the ``rifferr.image_resizers`` entry-point group and a
step picks one by name (``resizer:``).
"""

from __future__ import annotations

import importlib.metadata
import inspect
import io
import warnings
from collections.abc import Iterable
from typing import Any, Literal, Protocol, runtime_checkable

from rifferr.plugins import IMAGE_RESIZERS
from rifferr.steps.registry import EntryPoints

OutputFormat = Literal["keep", "jpeg"]


class ResizeError(Exception):
    """The image can't be decoded or resized."""


@runtime_checkable
class ImageResizer(Protocol):
    name: str

    def resize(self, data: bytes, max_side: int, fmt: OutputFormat, quality: int) -> bytes:
        """``data`` scaled down so its longest side is ``max_side``, aspect ratio kept.
        ``keep`` writes the input's format (JPEG or PNG); ``jpeg`` always writes JPEG at
        ``quality``. Raises ``ResizeError``."""
        ...


class PillowResizer:
    """Lanczos resampling; keeps the ICC profile; Pillow's decompression-bomb limit
    stays on, and an image over it fails (S-COVERS-RESIZE Security & safety)."""

    name = "pillow"

    def resize(self, data: bytes, max_side: int, fmt: OutputFormat, quality: int) -> bytes:
        from PIL import Image, ImageOps

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as opened:
                    source_format = opened.format
                    icc = opened.info.get("icc_profile")
                    image = ImageOps.exif_transpose(opened)
                    image.load()
        except (OSError, ValueError, Image.DecompressionBombWarning) as exc:
            raise ResizeError(f"can't read the image: {exc}") from exc
        except Image.DecompressionBombError as exc:
            raise ResizeError(f"image too large to decode: {exc}") from exc
        out_format = "JPEG" if fmt == "jpeg" or source_format != "PNG" else "PNG"
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if out_format == "JPEG" and image.mode not in ("RGB", "L", "CMYK"):
            image = image.convert("RGB")
        buf = io.BytesIO()
        options: dict[str, Any] = {}
        if icc:
            options["icc_profile"] = icc
        if out_format == "JPEG":
            options.update(quality=quality, optimize=True)
        else:
            options.update(optimize=True)
        try:
            image.save(buf, format=out_format, **options)
        except (OSError, ValueError) as exc:
            raise ResizeError(f"can't write the image: {exc}") from exc
        return buf.getvalue()


def _installed(group: str) -> Iterable[Any]:
    return importlib.metadata.entry_points(group=group)


def load_resizers(
    entry_points: EntryPoints = _installed,
) -> tuple[dict[str, ImageResizer], list[str]]:
    """Every installed resizer by name, and the errors of broken ones (never raised)."""
    found: dict[str, ImageResizer] = {}
    errors: list[str] = []
    for ep in entry_points(IMAGE_RESIZERS):
        try:
            obj = ep.load()
            resizer: Any = obj() if inspect.isclass(obj) else obj
        except Exception as exc:
            errors.append(f"image resizer {ep.name} ({ep.value}): failed to load: {exc}")
            continue
        if not isinstance(resizer, ImageResizer):
            errors.append(f"image resizer {ep.name} ({ep.value}): missing name or resize")
            continue
        found[ep.name] = resizer
    return found, errors
