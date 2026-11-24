"""Flexible attributes written as file tags (C-FILE-TAGS).

beets only writes the fields ``mediafile`` knows. A plugin may add more through
``BeetsPlugin.add_media_field`` (public beets API); after that every beets write path
(``Item.write``/``try_write``, ``beet write``, other ``beet`` subcommands) writes them, and
``Item.read`` reads them back. The ``rifferr`` beets plugin registers one media field per
``rifferr.file_tags`` entry of the beets config (FILE-TAGS-1).

beets dependencies pinned by tests/backends/beets/test_file_tags.py:
- ``BeetsPlugin.add_media_field`` (``MediaFile.add_field`` plus ``Item._media_fields``).
- ``BeetsPlugin.item_types`` / ``album_types`` and ``dbcore.types.MULTI_VALUE_DSV``.
- The ``write`` event's mutable ``tags`` dict, where ``None`` deletes a tag.
- ``mediafile`` storage styles: ``StorageStyle`` / ``ListStorageStyle`` (Vorbis, FLAC, APE),
  ``MP3DescStorageStyle`` / ``MP3ListDescStorageStyle`` (ID3 ``TXXX``), ``MP4StorageStyle`` /
  ``MP4ListStorageStyle`` (freeform atoms) and ``ASFStorageStyle``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Unpack

import beets
import mediafile
from beets.dbcore import types as dbtypes
from beets.events import WriteEventArgs
from beets.library import Album, Item
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

#: The beets config key under ``rifferr:``.
CONFIG_KEY = "file_tags"
DEFAULT_SEPARATOR = "; "
MP4_FREEFORM = "----:com.apple.iTunes:"

#: Fields this module added to ``MediaFile``, so a test (or a reload) can remove them.
_registered: dict[str, FileTagSpec] = {}


class FileTagError(Exception):
    """A ``rifferr.file_tags`` entry can't be used (FILE-TAGS-4)."""


class FileTagSpec(BaseModel, frozen=True):
    """One ``rifferr.file_tags`` entry (FILE-TAGS-2)."""

    model_config = ConfigDict(extra="forbid")

    tag: str
    multi: bool = False
    vorbis: str | None = None
    id3: str | None = None
    mp4: str | None = None
    ape: str | None = None
    asf: str | None = None

    @field_validator("tag", "vorbis", "id3", "mp4", "ape", "asf")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or not value.isprintable() or "=" in value:
            raise ValueError(f"{value!r} is not a usable tag name (printable, no '=')")
        return value


def parse(raw: Mapping[str, Any]) -> dict[str, FileTagSpec]:
    """``{field: "TAG" | {tag: ..., multi: ...}}`` → specs; errors name the field."""
    out: dict[str, FileTagSpec] = {}
    for name, value in raw.items():
        body = {"tag": value} if isinstance(value, str) else value
        try:
            out[str(name)] = FileTagSpec.model_validate(body)
        except ValidationError as exc:
            detail = "; ".join(e["msg"] for e in exc.errors())
            raise FileTagError(f"rifferr.file_tags.{name}: {detail}") from None
    return out


def from_config() -> tuple[dict[str, FileTagSpec], str]:
    """The entries and multi-value separator in the loaded beets config."""
    section = beets.config["rifferr"]
    raw = section[CONFIG_KEY].get() if section[CONFIG_KEY].exists() else None
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise FileTagError("rifferr.file_tags must be a mapping of field name to tag name")
    separator = DEFAULT_SEPARATOR
    if section["multi_separator"].exists():
        separator = str(section["multi_separator"].get())
        if not separator.strip():
            raise FileTagError("rifferr.multi_separator must not be blank")
    return parse(raw), separator  # pyright: ignore[reportUnknownArgumentType]


def _split(values: list[str], separator: str) -> list[str]:
    mark = separator.strip()
    out: list[str] = []
    for value in values:
        out.extend(part.strip() for part in value.split(mark) if part.strip())
    return out


class _JoinedID3List(mediafile.MP3ListDescStorageStyle):
    """``TXXX`` list: NUL-separated values in ID3v2.4; one joined value in ID3v2.3, which
    has no multiple values (FILE-TAGS-10). mutagen would otherwise join them with ``/``."""

    def __init__(self, desc: str, *, id3v23: bool, separator: str) -> None:
        self.id3v23 = id3v23
        self.separator = separator
        super().__init__(desc=desc)

    def fetch(self, mutagen_file: Any) -> list[str]:
        values = super().fetch(mutagen_file)
        if self.id3v23 or getattr(mutagen_file.tags, "version", None) == (2, 3, 0):
            return _split(list(values), self.separator)
        return values

    def store(self, mutagen_file: Any, values: list[str]) -> None:
        if self.id3v23 and len(values) > 1:
            values = [self.separator.join(values)]
        super().store(mutagen_file, values)


def descriptor(spec: FileTagSpec, *, id3v23: bool, separator: str) -> mediafile.MediaField:
    """Storage styles per format from the tag name, with overrides (FILE-TAGS-2, -3)."""
    vorbis = spec.vorbis or spec.tag
    id3 = spec.id3 or spec.tag
    mp4 = spec.mp4 or MP4_FREEFORM + spec.tag
    ape = spec.ape or spec.tag
    asf = spec.asf or spec.tag
    if spec.multi:
        return mediafile.ListMediaField(
            _JoinedID3List(id3, id3v23=id3v23, separator=separator),
            mediafile.MP4ListStorageStyle(mp4),
            _VorbisList(vorbis, ape),
            mediafile.ASFStorageStyle(asf),
        )
    return mediafile.MediaField(
        mediafile.MP3DescStorageStyle(id3),
        mediafile.MP4StorageStyle(mp4),
        _Vorbis(vorbis, ape),
        mediafile.ASFStorageStyle(asf),
    )


def _is_ape(mutagen_file: Any) -> bool:
    return type(mutagen_file).__name__ in {"APEv2File", "WavPack", "Musepack", "MonkeysAudio"}


class _Vorbis(mediafile.StorageStyle):
    """Vorbis comments (FLAC, Ogg) under one key, APEv2 under another."""

    def __init__(self, vorbis: str, ape: str) -> None:
        self.ape_key = ape
        super().__init__(vorbis)

    def _key(self, mutagen_file: Any) -> str:
        return self.ape_key if _is_ape(mutagen_file) else self.key

    def fetch(self, mutagen_file: Any) -> Any:
        try:
            return mutagen_file[self._key(mutagen_file)][0]
        except (KeyError, IndexError):
            return None

    def store(self, mutagen_file: Any, value: Any) -> None:
        mutagen_file[self._key(mutagen_file)] = [value]

    def delete(self, mutagen_file: Any) -> None:
        if self._key(mutagen_file) in mutagen_file:
            del mutagen_file[self._key(mutagen_file)]


class _VorbisList(mediafile.ListStorageStyle):
    """Repeated Vorbis comments (FILE-TAGS-3); APEv2 under its own key."""

    def __init__(self, vorbis: str, ape: str) -> None:
        self.ape_key = ape
        super().__init__(vorbis)

    def _key(self, mutagen_file: Any) -> str:
        return self.ape_key if _is_ape(mutagen_file) else self.key

    def fetch(self, mutagen_file: Any) -> list[Any]:
        try:
            return list(mutagen_file[self._key(mutagen_file)])
        except KeyError:
            return []

    def store(self, mutagen_file: Any, value: Any) -> None:
        mutagen_file[self._key(mutagen_file)] = value

    def delete(self, mutagen_file: Any) -> None:
        if self._key(mutagen_file) in mutagen_file:
            del mutagen_file[self._key(mutagen_file)]


def reserved(name: str) -> str | None:
    """Why ``name`` can't be mapped, or None (FILE-TAGS-4)."""
    if name in _registered:
        return None
    if name in mediafile.MediaFile.__dict__ or name in mediafile.MediaFile.fields():
        return "it is already a media file field"
    if name in Item._fields or name in Album._fields:  # pyright: ignore[reportPrivateUsage]
        return "it is a beets fixed field"
    return None


def unregister_all() -> None:
    """Remove every field this module added. For tests and reloads only."""
    for name in list(_registered):
        if name in mediafile.MediaFile.__dict__:
            delattr(mediafile.MediaFile, name)
        Item._media_fields.discard(name)  # pyright: ignore[reportPrivateUsage]
        del _registered[name]


def check(specs: Mapping[str, FileTagSpec]) -> None:
    """Raise for an entry that can't be mapped (FILE-TAGS-4)."""
    for name in specs:
        why = reserved(name)
        if why is not None:
            raise FileTagError(f"rifferr.file_tags.{name} can't be mapped: {why}")


def validate_config() -> None:
    """Before plugins load: beets logs and skips a plugin whose ``__init__`` raises, so
    the worker checks the entries itself and refuses to start (FILE-TAGS-4)."""
    unregister_all()
    specs, _separator = from_config()
    check(specs)


def register(plugin: Any, specs: Mapping[str, FileTagSpec], separator: str) -> None:
    """Add each field through ``plugin.add_media_field`` and give list fields a list type."""
    unregister_all()
    check(specs)
    id3v23 = bool(beets.config["id3v23"].get(bool))
    for name, spec in specs.items():
        plugin.add_media_field(name, descriptor(spec, id3v23=id3v23, separator=separator))
        _registered[name] = spec
        if spec.multi:  # FILE-TAGS-9
            plugin.item_types[name] = dbtypes.MULTI_VALUE_DSV
            plugin.album_types[name] = dbtypes.MULTI_VALUE_DSV


def registered() -> dict[str, FileTagSpec]:
    return dict(_registered)


def on_write(**event: Unpack[WriteEventArgs]) -> None:
    """``write`` event: an empty or absent mapped field removes its tag (FILE-TAGS-6)."""
    tags = event["tags"]
    for name in _registered:
        value = tags.get(name)
        if value is None or value == "" or value == []:
            tags[name] = None
