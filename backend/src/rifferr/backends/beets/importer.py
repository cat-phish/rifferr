"""ImportSessions: quiet (BEETS-IMPORT-4, -6, -7, -16) and, for F-IMPORT-UI, lookup with
deferred decisions, applying a decision, and looking a folder up again.

Mirrors the quiet behavior of beets' TerminalImportSession without a terminal:
strong matches are applied, everything else follows ``quiet_fallback``, and a
duplicate whose ``duplicate_action`` is ``ask`` is skipped.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from typing import Any

from beets import importer
from beets.autotag import AlbumMatch, Recommendation, TrackMatch, tag_album
from beets.importer import ImportAbortError, ImportTask, SingletonImportTask
from beets.library import Library
from beets.util import displayable_path

from rifferr.backends.base import ImportOutcome
from rifferr.backends.beets.snapshots import album_snapshot
from rifferr.core.imports import FolderItem, ImportCandidate, ImportQuestion
from rifferr.core.imports import TrackMatch as TrackMatchOut

Report = Callable[[ImportOutcome], None]


class QuietImportSession(importer.ImportSession):
    def __init__(
        self,
        lib: Library,
        path: bytes,
        *,
        should_stop: Callable[[], bool],
        report: Report,
    ) -> None:
        super().__init__(lib, None, [path])
        self._should_stop = should_stop
        self._report = report
        self._lock = threading.Lock()
        self.skipped: list[ImportOutcome] = []
        self.duplicates: list[ImportOutcome] = []
        self.stopped = False

    # -- decisions -------------------------------------------------------------

    def _decide(self, decision: ImportOutcome) -> None:
        with self._lock:
            (self.skipped if decision.outcome == "skipped" else self.duplicates).append(decision)
        self._report(decision)

    def _check_stop(self) -> None:
        if self._should_stop():
            self.stopped = True
            raise ImportAbortError()

    def _fallback(self, task: ImportTask, path: str) -> importer.Action:
        action = self.config["quiet_fallback"].as_choice(
            {"skip": importer.Action.SKIP, "asis": importer.Action.ASIS}
        )
        if action is importer.Action.SKIP:
            self._decide(
                ImportOutcome(
                    path=path, outcome="skipped", reason="no strong match (quiet_fallback: skip)"
                )
            )
        return action

    def choose_match(self, task: ImportTask) -> AlbumMatch | importer.Action:  # type: ignore[override]
        self._check_stop()
        if task.rec == Recommendation.strong and task.candidates:
            return task.candidates[0]  # type: ignore[return-value]
        return self._fallback(task, _paths(task.paths))

    def choose_item(self, task: SingletonImportTask) -> TrackMatch | importer.Action:  # type: ignore[override]
        self._check_stop()
        if task.rec == Recommendation.strong and task.candidates:
            return task.candidates[0]  # type: ignore[return-value]
        return self._fallback(task, os.fsdecode(task.item.path))

    def get_duplicate_action(self, task, found_duplicates):  # type: ignore[no-untyped-def]
        action = super().get_duplicate_action(task, found_duplicates)
        if action is importer.DuplicateAction.ASK:
            action = importer.DuplicateAction.SKIP
        path = _paths(task.paths)
        self._decide(ImportOutcome(path=path, outcome="duplicate", reason=action.name.lower()))
        if action is importer.DuplicateAction.SKIP:
            self._decide(
                ImportOutcome(
                    path=path, outcome="skipped", reason="duplicate of an album in the library"
                )
            )
        return action

    def should_resume(self, path: bytes) -> bool:
        return False  # never prompt; an interrupted import starts over

    def already_imported(self, toppath: bytes, paths: Sequence[bytes]) -> bool:
        self._check_stop()
        done = super().already_imported(toppath, paths)
        if done:
            self._decide(
                ImportOutcome(
                    path=_paths(paths), outcome="skipped", reason="already imported (incremental)"
                )
            )
        return done


def _paths(paths: Sequence[bytes]) -> str:
    return displayable_path(list(paths), "; ")


# -- import decisions (F-IMPORT-UI) ----------------------------------------------------

#: (paths) -> folder fingerprint; the backend gives it the filesystem helper.
Fingerprint = Callable[[str], str]


class DecidingImportSession(QuietImportSession):
    """Modes ``auto`` and ``interactive``: an album that needs a person is skipped and
    returned as an ``ImportQuestion`` (IMPORT-UI-1 .. -4). Its files stay untouched."""

    def __init__(
        self,
        lib: Library,
        path: bytes,
        *,
        ask_all: bool,
        ask_duplicates: bool,
        fingerprint: Fingerprint,
        should_stop: Callable[[], bool],
        report: Report,
    ) -> None:
        super().__init__(lib, path, should_stop=should_stop, report=report)
        self._ask_all = ask_all
        self._ask_duplicates = ask_duplicates
        self._fingerprint = fingerprint
        self.questions: list[ImportQuestion] = []

    def choose_match(self, task: ImportTask) -> AlbumMatch | importer.Action:  # type: ignore[override]
        self._check_stop()
        if not self._ask_all and task.rec == Recommendation.strong and task.candidates:
            return task.candidates[0]  # type: ignore[return-value]
        self._ask(task, "match", list(task.candidates or []))  # type: ignore[arg-type]
        return importer.Action.SKIP

    def get_duplicate_action(self, task, found_duplicates):  # type: ignore[no-untyped-def]
        if self._ask_duplicates:
            chosen = [task.match] if isinstance(task.match, AlbumMatch) else []
            self._ask(task, "duplicate", chosen, found_duplicates)
            return importer.DuplicateAction.SKIP
        return super().get_duplicate_action(task, found_duplicates)

    def _ask(
        self,
        task: ImportTask,
        kind: str,
        matches: list[AlbumMatch],
        duplicates: Sequence[Any] = (),
    ) -> None:
        folder = folder_of(task.paths)
        with self._lock:
            self.questions.append(
                ImportQuestion(
                    kind=kind,  # type: ignore[arg-type]
                    path=folder,
                    fingerprint=self._fingerprint(folder),
                    items=[folder_item(i) for i in task.items],
                    candidates=[candidate(m) for m in matches],
                    duplicates=[album_snapshot(a) for a in duplicates],
                )
            )


class AppliedImportSession(QuietImportSession):
    """Import one decided folder (IMPORT-UI-11): the candidate with ``candidate_id`` (the
    lookup is restricted to it with ``search_ids``), or as-is."""

    def __init__(
        self,
        lib: Library,
        path: bytes,
        *,
        candidate_id: str | None,
        should_stop: Callable[[], bool],
        report: Report,
    ) -> None:
        super().__init__(lib, path, should_stop=should_stop, report=report)
        self._candidate_id = candidate_id
        self.missing: str | None = None

    def choose_match(self, task: ImportTask) -> AlbumMatch | importer.Action:  # type: ignore[override]
        self._check_stop()
        if self._candidate_id is None:
            return importer.Action.ASIS
        for match in task.candidates or []:
            if isinstance(match, AlbumMatch) and candidate_id(match) == self._candidate_id:
                return match
        self.missing = f"candidate {self._candidate_id} was not found again"
        return importer.Action.SKIP

    def get_duplicate_action(self, task, found_duplicates):  # type: ignore[no-untyped-def]
        # The decision already answered any duplicate question (IMPORT-UI-3).
        return importer.ImportSession.get_duplicate_action(self, task, found_duplicates)


class LookupSession(QuietImportSession):
    """Look a folder up again without importing anything (IMPORT-UI-9)."""

    def __init__(
        self,
        lib: Library,
        path: bytes,
        *,
        ids: Sequence[str],
        artist: str | None,
        album: str | None,
    ) -> None:
        super().__init__(lib, path, should_stop=lambda: False, report=lambda d: None)
        self._ids = list(ids)
        self._artist = artist
        self._album = album
        self.found: list[ImportCandidate] = []
        self.items: list[FolderItem] = []

    def choose_match(self, task: ImportTask) -> AlbumMatch | importer.Action:  # type: ignore[override]
        if self._ids or self._artist or self._album:
            proposal = tag_album(
                task.source,
                search_artist=self._artist,
                search_name=self._album,
                search_ids=self._ids,
            )
            matches = list(proposal.candidates)
        else:
            matches = list(task.candidates or [])
        self.found.extend(candidate(m) for m in matches if isinstance(m, AlbumMatch))
        self.items.extend(folder_item(i) for i in task.items)
        return importer.Action.SKIP

    def get_duplicate_action(self, task, found_duplicates):  # type: ignore[no-untyped-def]
        return importer.DuplicateAction.SKIP

    def already_imported(self, toppath: bytes, paths: Sequence[bytes]) -> bool:
        return False


def folder_of(paths: Sequence[bytes]) -> str:
    """The album's folder: its one directory, or the folder holding its discs."""
    names = [os.fsdecode(p) for p in paths]
    if len(names) == 1:
        return names[0]
    return os.path.commonpath(names)


def candidate_id(match: AlbumMatch) -> str:
    info = match.info
    return f"{info.data_source}:{info.album_id}"


def _int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def candidate(match: AlbumMatch) -> ImportCandidate:
    """beets' ``AlbumMatch`` as backend-neutral data (IMPORT-UI-4)."""
    info = match.info
    mapping = [
        TrackMatchOut(
            item=os.fsdecode(item.path),
            title_before=_str(item.get("title")),
            title_after=_str(track.title),
            track=_int(track.index),
            length=track.length if isinstance(track.length, float | int) else None,
        )
        for item, track in match.mapping.items()
    ]
    distance = float(match.distance.distance)
    return ImportCandidate(
        id=candidate_id(match),
        source=str(info.data_source),
        artist=str(info.artist or ""),
        album=str(info.album or ""),
        year=_int(info.year),
        country=_str(info.country),
        label=_str(info.label),
        catalognum=_str(info.catalognum),
        media=_str(info.media),
        tracks=len(info.tracks),
        distance=min(max(distance, 0.0), 1.0),
        penalties=[key for key, _ in match.distance.items()],
        mapping=mapping,
        missing=[str(t.title or "") for t in match.extra_tracks],
        unmatched=[os.fsdecode(i.path) for i in match.extra_items],
        url=_str(info.get("data_url")),
    )


def folder_item(item: Any) -> FolderItem:
    tags = {
        k: item.get(k)
        for k in ("artist", "albumartist", "album", "title", "track", "disc", "year")
        if item.get(k) not in (None, "")
    }
    length = item.get("length")
    return FolderItem(
        path=os.fsdecode(item.path),
        format=_str(item.get("format")),
        length=float(length) if isinstance(length, int | float) else None,
        tags=tags,
    )
