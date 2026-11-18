"""S-LIBRARY-MOVE engine side: planning rules, journaled apply, recovery, undo (FakeBackend)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from rifferr.backends.base import FileMoveError
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSource,
    FieldChange,
    FileEffect,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from rifferr.core.moves import ArtifactRule, MovePlan, PathFormat, artifact_destination
from rifferr.core.selection import Selection
from rifferr.engine import InMemoryJournal
from rifferr.engine.changesets import ApplyOutcome, InMemoryChangeSetStore, apply_changeset
from rifferr.engine.recovery import recover_pending
from rifferr.engine.undo import UndoContext, undo_entry

RUN = "run-1"
SRC = "/inbox/Abbey Road"
IMAGES = ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.tif", "*.tiff", "*.bmp"]
REFERENCE_RULES = [ArtifactRule(match=IMAGES, to="Art"), ArtifactRule(match=["*"])]


class Lib:
    def __init__(self) -> None:
        self.backend = FakeBackend()
        self.backend.add_album(
            {
                "albumartist": "The Beatles",
                "album": "Abbey Road",
                "rifferr_run": RUN,
                "path": SRC,
                "artpath": f"{SRC}/cover.jpg",
                "items": [{"title": f"T{n}", "track": n} for n in range(1, 4)],
            }
        )
        self.backend.album_files(self.album.id)
        self.journal = InMemoryJournal()
        self.store = InMemoryChangeSetStore()
        self.events: list[tuple[str, dict[str, JsonValue]]] = []
        self._ids = count(1)
        self._t = datetime(2026, 11, 18, tzinfo=UTC)

    def new_id(self) -> str:
        return f"id-{next(self._ids):03d}"

    def now(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t

    def emitter_for(self, run_id: str | None, step_id: str | None) -> Any:
        return lambda type, data: self.events.append((type, data))

    @property
    def album(self) -> Any:
        return self.backend.query(Selection())[0]

    def plans(self, mode: str = "move", **kw: Any) -> list[MovePlan]:
        kw.setdefault("formats", None)
        kw.setdefault("directory", None)
        return self.backend.plan_moves(Selection(run_id=RUN), mode=mode, **kw)  # type: ignore[arg-type]

    def changeset(self, plan: MovePlan, stamp: list[TargetChange] | None = None) -> ChangeSet:
        cs = ChangeSet(
            id=self.new_id(),
            profile="main",
            run_id=RUN,
            step_id="into-library",
            source=ChangeSource(kind="step", name="library.move"),
            created_at=self.now(),
            targets=stamp or [],
            files=plan.files,
        )
        self.store.add(cs)
        return cs

    def apply(self, cs: ChangeSet) -> ApplyOutcome:
        return apply_changeset(
            cs,
            backend=self.backend,
            journal=self.journal,
            store=self.store,
            new_id=self.new_id,
            now=self.now,
            emit=self.emitter_for(RUN, "into-library"),
        )

    def move(self, mode: str = "move", **kw: Any) -> ApplyOutcome:
        [plan] = self.plans(mode, **kw)
        assert not plan.conflicts
        return self.apply(self.changeset(plan))

    def ctx(self) -> UndoContext:
        return UndoContext(
            backend=self.backend,
            journal=self.journal,
            store=self.store,
            new_id=self.new_id,
            now=self.now,
            emitter_for=self.emitter_for,
            by="tester",
        )

    def paths(self) -> list[str]:
        return [i.path for i in self.album.items]


DEST = "/library/The Beatles/Abbey Road"


@pytest.fixture
def lib() -> Lib:
    return Lib()


# -- planning ---------------------------------------------------------------------


def test_LIBRARY_MOVE_3_plan_lists_items_and_art_with_destinations(lib: Lib) -> None:
    [plan] = lib.plans()
    assert plan.src_dir == SRC
    assert plan.dst_dir == DEST
    assert [(f.kind, f.dst) for f in plan.files] == [
        ("item", f"{DEST}/1 T1.flac"),
        ("item", f"{DEST}/2 T2.flac"),
        ("item", f"{DEST}/3 T3.flac"),
        ("art", f"{DEST}/cover.jpg"),
    ]
    assert all(f.state == "planned" and f.mode == "move" for f in plan.files)
    assert plan.files[0].size == len(f"{SRC}/01 T1.flac")


def test_LIBRARY_MOVE_3_album_already_in_place_plans_no_files(lib: Lib) -> None:
    lib.move()
    [plan] = lib.plans()
    assert plan.files == []
    assert plan.conflicts == []


def test_LIBRARY_MOVE_4_planning_only_reads(lib: Lib) -> None:
    before = dict(lib.backend.fs)
    lib.plans()
    assert lib.backend.fs == before
    assert lib.paths() == [f"{SRC}/0{n} T{n}.flac" for n in (1, 2, 3)]


def test_LIBRARY_MOVE_5_destination_outside_directory_is_a_conflict(lib: Lib) -> None:
    [plan] = lib.plans(formats=[PathFormat(query="default", format="../escape/$title")])
    assert any("outside /library" in c for c in plan.conflicts)


def test_LIBRARY_MOVE_5_two_files_to_one_destination_is_a_conflict(lib: Lib) -> None:
    [plan] = lib.plans(formats=[PathFormat(query="default", format="$albumartist/$album/x")])
    assert any("both go to" in c for c in plan.conflicts)


def test_LIBRARY_MOVE_5_existing_destination_file_is_a_conflict(lib: Lib) -> None:
    lib.backend.add_files({f"{DEST}/1 T1.flac": "someone else's"})
    [plan] = lib.plans()
    assert any("already exists" in c for c in plan.conflicts)


def test_LIBRARY_MOVE_5_existing_album_folder_is_a_conflict_even_without_name_clash(
    lib: Lib,
) -> None:
    lib.backend.add_files({f"{DEST}/other.flac": "another album"})
    [plan] = lib.plans()
    assert any("destination folder" in c and "already exists" in c for c in plan.conflicts)


def test_LIBRARY_MOVE_6_reference_artifact_rules(lib: Lib) -> None:
    lib.backend.add_files(
        {f"{SRC}/back.jpg": "b", f"{SRC}/scans/cd.png": "c", f"{SRC}/rip.log": "l"}
    )
    [plan] = lib.plans(artifacts=REFERENCE_RULES)
    moved = {f.src.removeprefix(SRC + "/"): f.dst.removeprefix(DEST + "/") for f in plan.files}
    assert moved["cover.jpg"] == "cover.jpg"  # beets art: kind art, never an artifact
    assert moved["back.jpg"] == "Art/back.jpg"
    assert moved["scans/cd.png"] == "Art/cd.png"
    assert moved["rip.log"] == "rip.log"
    kinds = {f.src.removeprefix(SRC + "/"): f.kind for f in plan.files}
    assert kinds["cover.jpg"] == "art"
    assert kinds["back.jpg"] == "artifact"


def test_LIBRARY_MOVE_6_no_rules_moves_audio_and_art_only(lib: Lib) -> None:
    lib.backend.add_files({f"{SRC}/rip.log": "l"})
    [plan] = lib.plans(artifacts=[])
    assert {f.kind for f in plan.files} == {"item", "art"}


def test_LIBRARY_MOVE_6_unmatched_files_stay_and_name_clashes_conflict(lib: Lib) -> None:
    lib.backend.add_files({f"{SRC}/a/x.png": "1", f"{SRC}/b/x.png": "2", f"{SRC}/notes.txt": "n"})
    [plan] = lib.plans(artifacts=[ArtifactRule(match=["*.png"], to="Art")])
    assert not any(f.src.endswith("notes.txt") for f in plan.files)
    assert any("both go to" in c for c in plan.conflicts)


def test_LIBRARY_MOVE_6_matching_is_case_insensitive_first_rule_wins() -> None:
    assert artifact_destination("Scans/CD.PNG", REFERENCE_RULES) == "Art/CD.PNG"
    assert artifact_destination("rip.log", REFERENCE_RULES) == "rip.log"
    assert artifact_destination("rip.log", [ArtifactRule(match=["*.png"])]) is None


def test_LIBRARY_MOVE_6_to_must_stay_inside_the_album_folder() -> None:
    for bad in ("../x", "/abs", "a/../../x"):
        with pytest.raises(ValidationError):
            ArtifactRule(match=["*"], to=bad)


# -- apply --------------------------------------------------------------------------


def test_LIBRARY_MOVE_7_journal_lists_every_file_planned_before_the_first_is_touched(
    lib: Lib,
) -> None:
    seen: list[JournalEntry] = []
    real = lib.backend.copy_file

    def spy(effect: FileEffect) -> FileEffect:
        if not seen:
            seen.extend(lib.journal.entries.values())
        return real(effect)

    lib.backend.copy_file = spy  # type: ignore[method-assign]
    lib.move()
    [entry] = seen
    assert entry.state == "pending"
    assert len(entry.files) == 4
    assert {f.state for f in entry.files} == {"planned"}
    assert all(len(f.sha256) == 64 for f in entry.files)


def test_LIBRARY_MOVE_8_move_copies_verifies_then_removes_source(lib: Lib) -> None:
    outcome = lib.move()
    assert outcome.state == "applied"
    assert outcome.error is None
    assert lib.paths() == [f"{DEST}/{n} T{n}.flac" for n in (1, 2, 3)]
    assert not any(p.startswith(SRC) for p in lib.backend.fs)
    assert lib.backend.fs[f"{DEST}/cover.jpg"] == f"{SRC}/cover.jpg".encode()


def test_LIBRARY_MOVE_8_copy_keeps_the_source(lib: Lib) -> None:
    outcome = lib.move("copy")
    assert outcome.state == "applied"
    assert f"{SRC}/01 T1.flac" in lib.backend.fs
    assert f"{DEST}/1 T1.flac" in lib.backend.fs
    assert lib.paths()[0] == f"{DEST}/1 T1.flac"


def test_LIBRARY_MOVE_8_same_filesystem_move_may_rename(lib: Lib) -> None:
    lib.backend.same_filesystem = True
    assert lib.move().state == "applied"
    assert not any(p.startswith(SRC) for p in lib.backend.fs)


def test_LIBRARY_MOVE_8_a_source_that_changed_since_planning_is_not_copied(lib: Lib) -> None:
    [plan] = lib.plans()
    lib.backend.fs[f"{SRC}/01 T1.flac"] = b"changed"
    outcome = lib.apply(lib.changeset(plan))
    assert outcome.error is not None
    assert "changed" in outcome.error
    assert f"{DEST}/1 T1.flac" not in lib.backend.fs


def test_LIBRARY_MOVE_9_states_are_journaled_and_path_changes_after_verify(lib: Lib) -> None:
    outcome = lib.move()
    assert outcome.entry is not None
    assert {f.state for f in outcome.entry.files} == {"done"}
    first = lib.backend.set_paths_calls[0][0]
    assert first.state == "copied"  # verified copy, source not yet removed


def test_LIBRARY_MOVE_10_failure_stops_at_that_file_and_marks_partial(lib: Lib) -> None:
    lib.backend.fail_files[f"{SRC}/02 T2.flac"] = "disk full"
    outcome = lib.move()
    assert outcome.entry is not None
    assert outcome.entry.state == "partial"
    assert outcome.state == "partially_applied"
    assert outcome.error is not None
    assert "disk full" in outcome.error
    assert [f.state for f in outcome.entry.files] == ["done", "planned", "planned", "planned"]
    assert lib.paths()[0].startswith(DEST)
    assert lib.paths()[1].startswith(SRC)


def test_LIBRARY_MOVE_10_failure_on_the_first_file_changes_nothing(lib: Lib) -> None:
    lib.backend.fail_files[f"{SRC}/01 T1.flac"] = "denied"
    outcome = lib.move()
    assert outcome.entry is not None
    assert outcome.entry.state == "failed"
    assert outcome.state == "conflicted"


def _stamp(lib: Lib) -> list[TargetChange]:
    album = TargetRef(scope="album", id=lib.album.id)
    return [TargetChange(target=album, changes=[FieldChange(field="date_added", new="2026-11-18")])]


def test_LIBRARY_MOVE_6b_stamp_is_written_once_all_files_are_done(lib: Lib) -> None:
    [plan] = lib.plans()
    outcome = lib.apply(lib.changeset(plan, _stamp(lib)))
    assert outcome.state == "applied"
    assert lib.album.fields["date_added"] == "2026-11-18"
    assert outcome.entry is not None
    assert outcome.entry.effects


def test_LIBRARY_MOVE_6b_no_stamp_when_a_file_fails(lib: Lib) -> None:
    lib.backend.fail_files[f"{SRC}/03 T3.flac"] = "io error"
    [plan] = lib.plans()
    lib.apply(lib.changeset(plan, _stamp(lib)))
    assert "date_added" not in lib.album.fields


# -- recovery -----------------------------------------------------------------------


def _crash_after(lib: Lib, n: int, mode: str = "move") -> JournalEntry:
    """A pending entry whose first ``n`` files were copied, as a crashed worker leaves it."""
    [plan] = lib.plans(mode)
    cs = lib.changeset(plan)
    entry = JournalEntry(
        id=lib.new_id(),
        profile="main",
        changeset_id=cs.id,
        run_id=RUN,
        intended=[],
        files=plan.files,
        created_at=lib.now(),
    )
    lib.journal.begin(entry)
    for f in plan.files[:n]:
        lib.backend.copy_file(f)
    return entry


def recover(lib: Lib) -> list[JournalEntry]:
    return recover_pending(
        "main",
        journal=lib.journal,
        backend=lib.backend,
        store=lib.store,
        emitter_for=lib.emitter_for,
        now=lib.now,
    )


def test_LIBRARY_MOVE_12_recovery_reconciles_from_the_filesystem(lib: Lib) -> None:
    entry = _crash_after(lib, 2)
    lib.backend.fs.pop(entry.files[1].src)  # its source removal had happened
    [done] = recover(lib)
    assert done.state == "partial"
    assert [f.state for f in done.files] == ["copied", "done", "planned", "planned"]
    assert lib.paths()[:2] == [f"{DEST}/1 T1.flac", f"{DEST}/2 T2.flac"]
    assert entry.files[0].src in lib.backend.fs  # nothing deleted by recovery


def test_LIBRARY_MOVE_12_nothing_copied_settles_failed(lib: Lib) -> None:
    _crash_after(lib, 0)
    [done] = recover(lib)
    assert done.state == "failed"


# -- undo ---------------------------------------------------------------------------


def test_LIBRARY_MOVE_13_undo_moves_everything_back(lib: Lib) -> None:
    before = dict(lib.backend.fs)
    outcome = lib.move()
    assert outcome.entry is not None
    undo = undo_entry(outcome.entry.id, lib.ctx())
    assert undo.undone
    assert undo.skipped_files == []
    assert lib.backend.fs == before
    assert lib.paths() == [f"{SRC}/0{n} T{n}.flac" for n in (1, 2, 3)]
    assert lib.journal.entries[outcome.entry.id].undone_by == undo.undo_entry_id


def test_LIBRARY_MOVE_13_missing_library_copy_is_skipped_and_undo_incomplete(lib: Lib) -> None:
    outcome = lib.move()
    assert outcome.entry is not None
    del lib.backend.fs[f"{DEST}/2 T2.flac"]
    undo = undo_entry(outcome.entry.id, lib.ctx())
    assert not undo.undone
    assert undo.skipped_files
    assert "missing" in undo.skipped_files[0]
    assert lib.journal.entries[outcome.entry.id].undone_by is None
    assert any(t == "changeset.undo_incomplete" for t, _ in lib.events)


def test_LIBRARY_MOVE_13_partial_entries_can_be_undone(lib: Lib) -> None:
    lib.backend.fail_files[f"{SRC}/02 T2.flac"] = "disk full"
    outcome = lib.move()
    del lib.backend.fail_files[f"{SRC}/02 T2.flac"]
    assert outcome.entry is not None
    assert undo_entry(outcome.entry.id, lib.ctx()).undone
    assert lib.paths()[0] == f"{SRC}/01 T1.flac"


def test_LIBRARY_MOVE_14_undo_of_copy_removes_the_library_copy_only(lib: Lib) -> None:
    before = dict(lib.backend.fs)
    outcome = lib.move("copy")
    assert outcome.entry is not None
    assert undo_entry(outcome.entry.id, lib.ctx()).undone
    assert lib.backend.fs == before
    assert lib.paths()[0] == f"{SRC}/01 T1.flac"


def test_LIBRARY_MOVE_14_undo_never_deletes_the_only_copy(lib: Lib) -> None:
    outcome = lib.move("copy")
    assert outcome.entry is not None
    del lib.backend.fs[f"{SRC}/01 T1.flac"]  # the original is gone
    undo = undo_entry(outcome.entry.id, lib.ctx())
    assert not undo.undone
    assert f"{DEST}/1 T1.flac" in lib.backend.fs
    assert any("only copy" in s for s in undo.skipped_files)


def test_LIBRARY_MOVE_15_undo_refuses_when_a_moved_file_changed(lib: Lib) -> None:
    outcome = lib.move()
    assert outcome.entry is not None
    lib.backend.fs[f"{DEST}/1 T1.flac"] = b"retagged"
    undo = undo_entry(outcome.entry.id, lib.ctx())
    assert not undo.undone
    assert undo.changed_files == [f"{DEST}/1 T1.flac"]
    assert lib.paths()[0] == f"{DEST}/1 T1.flac"  # nothing undone


def test_LIBRARY_MOVE_8_copy_never_overwrites(lib: Lib) -> None:
    [plan] = lib.plans()
    lib.backend.add_files({plan.files[0].dst: "late arrival"})
    with pytest.raises(FileMoveError):
        lib.backend.copy_file(plan.files[0])
