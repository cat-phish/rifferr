"""Quiet ImportSession (BEETS-IMPORT-4, -6, -7, -16). Decisions for other modes: M2.

Mirrors the quiet behavior of beets' TerminalImportSession without a terminal:
strong matches are applied, everything else follows ``quiet_fallback``, and a
duplicate whose ``duplicate_action`` is ``ask`` is skipped.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence

from beets import importer
from beets.autotag import AlbumMatch, Recommendation, TrackMatch
from beets.importer import ImportAbortError, ImportTask, SingletonImportTask
from beets.library import Library
from beets.util import displayable_path

from rifferr.backends.base import ImportDecision

Report = Callable[[ImportDecision], None]


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
        self.skipped: list[ImportDecision] = []
        self.duplicates: list[ImportDecision] = []
        self.stopped = False

    # -- decisions -------------------------------------------------------------

    def _decide(self, decision: ImportDecision) -> None:
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
                ImportDecision(
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
        self._decide(ImportDecision(path=path, outcome="duplicate", reason=action.name.lower()))
        if action is importer.DuplicateAction.SKIP:
            self._decide(
                ImportDecision(
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
                ImportDecision(
                    path=_paths(paths), outcome="skipped", reason="already imported (incremental)"
                )
            )
        return done


def _paths(paths: Sequence[bytes]) -> str:
    return displayable_path(list(paths), "; ")
