"""C-JOURNAL: start-up reconciliation (JOURNAL-3) and undo (JOURNAL-4 .. -7, -9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from pydantic import JsonValue

from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSource,
    FieldChange,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from rifferr.core.selection import Selection
from rifferr.engine import InMemoryJournal
from rifferr.engine.changesets import InMemoryChangeSetStore, apply_changeset
from rifferr.engine.recovery import INTERRUPTED, reconcile, recover_pending
from rifferr.engine.undo import UndoContext, UndoError, inverse, undo_entry, undo_run

RUN = "run-1"


class Lib:
    """The spec's example: album-7 'Live Sessions' with 4 tracks."""

    def __init__(self) -> None:
        self.backend = FakeBackend()
        self.backend.add_album(
            {
                "album": "Live Sessions",
                "year": 2001,
                "rifferr_run": RUN,
                "items": [{"title": f"T{n}", "track": n} for n in range(1, 5)],
            }
        )
        self.journal = InMemoryJournal()
        self.store = InMemoryChangeSetStore()
        self.events: list[tuple[str, str | None, dict[str, JsonValue]]] = []
        self._ids = count(1)
        self._t = datetime(2026, 11, 12, tzinfo=UTC)

    def new_id(self) -> str:
        return f"id-{next(self._ids):03d}"

    def now(self) -> datetime:
        self._t += timedelta(seconds=1)
        return self._t

    def emitter_for(self, run_id: str | None, step_id: str | None) -> Any:
        return lambda type, data: self.events.append((type, run_id, data))

    @property
    def album(self) -> Any:
        return self.backend.query(Selection())[0]

    def ref(self, item: int | None = None) -> TargetRef:
        if item is None:
            return TargetRef(scope="album", id=self.album.id)
        return TargetRef(scope="item", id=self.album.items[item].id)

    def changeset(self, *targets: tuple[TargetRef, list[FieldChange]]) -> ChangeSet:
        cs = ChangeSet(
            id=self.new_id(),
            profile="main",
            run_id=RUN,
            step_id="p",
            source=ChangeSource(kind="step", name="test"),
            created_at=self.now(),
            targets=[TargetChange(target=t, changes=c) for t, c in targets],
        )
        self.store.add(cs)
        return cs

    def apply(self, *targets: tuple[TargetRef, list[FieldChange]]) -> JournalEntry:
        cs = self.changeset(*targets)
        outcome = apply_changeset(
            cs,
            backend=self.backend,
            journal=self.journal,
            store=self.store,
            new_id=self.new_id,
            now=self.now,
            emit=self.emitter_for(RUN, "p"),
        )
        assert outcome.entry is not None
        return outcome.entry

    def rename(self, old: str = "Live Sessions", new: str = "Live Sessions (EP)") -> JournalEntry:
        return self.apply((self.ref(), [FieldChange(field="album", old=old, new=new)]))

    def edit(self, ref: TargetRef, field: str, old: Any, new: Any) -> None:
        """Someone else edits the library (straight to the backend)."""
        cs = ChangeSet(
            id="outside",
            profile="main",
            source=ChangeSource(kind="manual", name="test"),
            created_at=self.now(),
            targets=[
                TargetChange(target=ref, changes=[FieldChange(field=field, old=old, new=new)])
            ],
        )
        assert self.backend.apply(cs).applied

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

    def names(self) -> list[Any]:
        album = self.album
        return [album.fields["album"], *(i.fields["album"] for i in album.items)]

    def pending(self, *targets: tuple[TargetRef, list[FieldChange]]) -> JournalEntry:
        """A begun but never completed apply, as a crashed worker leaves it."""
        cs = self.changeset(*targets)
        entry = JournalEntry(
            id=self.new_id(),
            profile="main",
            changeset_id=cs.id,
            run_id=RUN,
            intended=cs.targets,
            created_at=self.now(),
        )
        self.journal.begin(entry)
        return entry

    def recover(self) -> list[JournalEntry]:
        return recover_pending(
            "main",
            journal=self.journal,
            backend=self.backend,
            store=self.store,
            emitter_for=self.emitter_for,
            now=self.now,
        )


@pytest.fixture
def lib() -> Lib:
    return Lib()


# -- JOURNAL-4, -5: undo of an entry ----------------------------------------------------


def test_JOURNAL_4_undo_restores_every_effect_including_propagated_ones(lib: Lib) -> None:
    e1 = lib.rename()
    assert len(e1.effects) == 5  # the album and its 4 items
    outcome = undo_entry(e1.id, lib.ctx())
    assert outcome.undone
    assert lib.names() == ["Live Sessions"] * 5
    e2 = lib.journal.get(str(outcome.undo_entry_id))
    assert e2 is not None
    assert e2.undo_of == e1.id
    assert e2.state == "applied"
    original = lib.journal.get(e1.id)
    assert original is not None
    assert original.undone_by == e2.id


def test_JOURNAL_4_inverse_expects_after_and_restores_before(lib: Lib) -> None:
    e1 = lib.rename()
    targets = inverse(e1.effects)
    assert len(targets) == 5
    assert targets[0].target.scope == "album"
    change = targets[0].changes[0]
    assert (change.op, change.old, change.new) == ("set", "Live Sessions (EP)", "Live Sessions")


def test_JOURNAL_4_fields_that_did_not_exist_are_deleted(lib: Lib) -> None:
    e1 = lib.apply((lib.ref(0), [FieldChange(field="mood", old=None, new="calm")]))
    (change,) = inverse(e1.effects)[0].changes
    assert (change.op, change.old) == ("delete", "calm")
    assert undo_entry(e1.id, lib.ctx()).undone
    assert "mood" not in lib.album.items[0].fields


def test_JOURNAL_4_undo_is_a_manual_changeset_named_undo(lib: Lib) -> None:
    e1 = lib.rename()
    outcome = undo_entry(e1.id, lib.ctx())
    e2 = lib.journal.get(str(outcome.undo_entry_id))
    assert e2 is not None
    stored = lib.store.get(e2.changeset_id)
    assert stored is not None
    assert (stored.changeset.source.kind, stored.changeset.source.name) == ("manual", "undo")
    assert stored.state == "applied"
    assert stored.resolved_by == "tester"
    original = lib.store.get(e1.changeset_id)
    assert original is not None
    assert original.state == "undone"
    types = [t for t, _, _ in lib.events]
    assert types[-1] == "changeset.undone"


def test_JOURNAL_5_conflict_undoes_nothing_and_reports_what_it_would_restore(lib: Lib) -> None:
    e1 = lib.rename()
    lib.edit(lib.ref(1), "album", "Live Sessions (EP)", "LS")
    outcome = undo_entry(e1.id, lib.ctx())
    assert not outcome.undone
    (conflict,) = outcome.conflicts
    assert conflict.target == lib.ref(1)
    assert (conflict.expected, conflict.current, conflict.would_restore) == (
        "Live Sessions (EP)",
        "LS",
        "Live Sessions",
    )
    assert lib.names() == [
        "Live Sessions (EP)",
        "Live Sessions (EP)",
        "LS",
        *["Live Sessions (EP)"] * 2,
    ]
    original = lib.journal.get(e1.id)
    assert original is not None
    assert original.undone_by is None


def test_JOURNAL_5_an_undone_entry_is_not_undone_again(lib: Lib) -> None:
    e1 = lib.rename()
    undo_entry(e1.id, lib.ctx())
    with pytest.raises(UndoError, match="already undone"):
        undo_entry(e1.id, lib.ctx())
    with pytest.raises(UndoError, match="no journal entry"):
        undo_entry("missing", lib.ctx())


def test_JOURNAL_5_failed_entries_cannot_be_undone(lib: Lib) -> None:
    e1 = lib.rename(old="wrong")  # conflicts, so the entry failed
    assert e1.state == "failed"
    with pytest.raises(UndoError, match="failed"):
        undo_entry(e1.id, lib.ctx())


def test_JOURNAL_5_undoing_the_undo_reapplies(lib: Lib) -> None:
    e1 = lib.rename()
    e2 = str(undo_entry(e1.id, lib.ctx()).undo_entry_id)
    e3 = undo_entry(e2, lib.ctx())
    assert e3.undone
    assert lib.names() == ["Live Sessions (EP)"] * 5


# -- JOURNAL-6: undo of a run ----------------------------------------------------------


def test_JOURNAL_6_run_undo_goes_newest_first(lib: Lib) -> None:
    first = lib.rename()
    second = lib.apply((lib.ref(), [FieldChange(field="year", old=2001, new=2002)]))
    result = undo_run(RUN, lib.ctx())
    assert [o.entry_id for o in result.undone] == [second.id, first.id]
    assert result.stopped is None
    assert lib.album.fields["album"] == "Live Sessions"
    assert lib.album.fields["year"] == 2001
    assert result.as_json()["undone"] == [second.id, first.id]


def test_JOURNAL_6_run_undo_stops_at_the_first_conflict(lib: Lib) -> None:
    first = lib.rename()
    second = lib.apply((lib.ref(), [FieldChange(field="year", old=2001, new=2002)]))
    third = lib.apply((lib.ref(2), [FieldChange(field="title", old="T3", new="Three")]))
    lib.edit(lib.ref(), "year", 2002, 2010)
    result = undo_run(RUN, lib.ctx())
    assert [o.entry_id for o in result.undone] == [third.id]
    assert result.stopped is not None
    assert result.stopped.entry_id == second.id
    conflicts = result.as_json()["conflicts"]
    assert isinstance(conflicts, list)
    assert len(conflicts) == 5  # the outside edit reached the 4 items too
    assert conflicts[0] == {
        "target": f"album {lib.album.id}",
        "field": "year",
        "expected": 2002,
        "current": 2010,
        "would_restore": 2001,
    }
    assert lib.album.fields["album"] == "Live Sessions (EP)"  # first left alone
    untouched = lib.journal.get(first.id)
    assert untouched is not None
    assert untouched.undone_by is None


def test_JOURNAL_6_run_undo_skips_undone_entries_and_undos(lib: Lib) -> None:
    first = lib.rename()
    lib.apply((lib.ref(), [FieldChange(field="year", old=2001, new=2002)]))
    undo_entry(first.id, lib.ctx())
    result = undo_run(RUN, lib.ctx())
    assert len(result.undone) == 1
    assert lib.album.fields["year"] == 2001
    assert lib.album.fields["album"] == "Live Sessions"
    assert undo_run(RUN, lib.ctx()).undone == []


# -- JOURNAL-7: write failures ---------------------------------------------------------


def test_JOURNAL_7_write_failures_stay_with_the_entry_and_undo_rewrites(lib: Lib) -> None:
    broken = lib.album.items[0].id
    lib.backend.fail_writes[broken] = "read-only"
    e1 = lib.rename()
    stored = lib.journal.get(e1.id)
    assert stored is not None
    assert [f.target.id for f in stored.write_failures] == [broken]
    lib.backend.fail_writes.clear()
    outcome = undo_entry(e1.id, lib.ctx())
    e2 = lib.journal.get(str(outcome.undo_entry_id))
    assert e2 is not None
    assert e2.write_failures == []  # the undo went through the normal apply path


# -- JOURNAL-3: start-up reconciliation -------------------------------------------------


def _rename_change(lib: Lib) -> tuple[TargetRef, list[FieldChange]]:
    return (lib.ref(), [FieldChange(field="album", old="Live Sessions", new="LS (EP)")])


def test_JOURNAL_3_interrupted_before_apply_fails_and_reproposes(lib: Lib) -> None:
    entry = lib.pending(_rename_change(lib))
    (done,) = lib.recover()
    assert (done.state, done.error) == ("failed", INTERRUPTED)
    stored = lib.store.get(entry.changeset_id)
    assert stored is not None
    assert stored.state == "proposed"
    assert lib.journal.pending("main") == []
    assert lib.backend.apply_calls == []  # recovery never writes


def test_JOURNAL_3_applied_before_the_crash_becomes_applied(lib: Lib) -> None:
    entry = lib.pending(_rename_change(lib))
    lib.edit(lib.ref(), "album", "Live Sessions", "LS (EP)")  # the apply that happened
    lib.backend.apply_calls.clear()
    (done,) = lib.recover()
    assert done.state == "applied"
    assert not done.uncertain
    assert [(e.before, e.after) for e in done.effects] == [("Live Sessions", "LS (EP)")]
    stored = lib.store.get(entry.changeset_id)
    assert stored is not None
    assert stored.state == "applied"
    assert lib.backend.apply_calls == []
    assert lib.events[-1][:2] == ("changeset.applied", RUN)


def test_JOURNAL_3_anything_else_is_uncertain_and_goes_to_review(lib: Lib) -> None:
    entry = lib.pending(
        (
            lib.ref(),
            [
                FieldChange(field="album", old="Live Sessions", new="LS (EP)"),
                FieldChange(field="year", old=2001, new=2002),
            ],
        )
    )
    lib.edit(lib.ref(), "album", "Live Sessions", "LS (EP)")  # only half of it
    (done,) = lib.recover()
    assert done.state == "applied"
    assert done.uncertain
    assert [e.field for e in done.effects] == ["album"]
    assert [(c.field, c.expected, c.current) for c in done.conflicts] == [("year", 2002, 2001)]
    assert [e.id for e in lib.journal.uncertain("main")] == [entry.id]


def test_JOURNAL_3_reconcile_is_pure(lib: Lib) -> None:
    entry = lib.pending(_rename_change(lib))
    now = datetime(2026, 11, 12, tzinfo=UTC)
    done = reconcile(entry, lambda ref: {"album": "LS (EP)"}, now)
    assert done.state == "applied"
    assert done.completed_at == now
    assert lib.journal.pending("main") == [entry]  # nothing stored by reconcile


def test_JOURNAL_3_interrupted_undo_is_dropped(lib: Lib) -> None:
    e1 = lib.rename()
    cs = lib.changeset(
        (lib.ref(), [FieldChange(field="album", old="Live Sessions (EP)", new="Live Sessions")])
    )
    entry = JournalEntry(
        id="undo-pending",
        profile="main",
        changeset_id=cs.id,
        run_id=RUN,
        intended=cs.targets,
        undo_of=e1.id,
        created_at=lib.now(),
    )
    lib.journal.begin(entry)
    (done,) = lib.recover()
    assert done.state == "failed"
    stored = lib.store.get(cs.id)
    assert stored is not None
    assert stored.state == "rejected"
    original = lib.journal.get(e1.id)
    assert original is not None
    assert original.undone_by is None


def test_JOURNAL_3_undo_that_completed_before_the_crash_marks_the_original(lib: Lib) -> None:
    e1 = lib.rename()
    targets = inverse(e1.effects)
    cs = lib.changeset(*((t.target, list(t.changes)) for t in targets))
    entry = JournalEntry(
        id="undo-done",
        profile="main",
        changeset_id=cs.id,
        run_id=RUN,
        intended=cs.targets,
        undo_of=e1.id,
        created_at=lib.now(),
    )
    lib.journal.begin(entry)
    lib.backend.apply(cs)  # the undo itself reached the library
    lib.recover()
    original = lib.journal.get(e1.id)
    assert original is not None
    assert original.undone_by == "undo-done"
    stored = lib.store.get(e1.changeset_id)
    assert stored is not None
    assert stored.state == "undone"


# -- JOURNAL-9: contents and retention ---------------------------------------------------


def test_JOURNAL_9_entries_hold_tag_values_only_and_are_kept(lib: Lib) -> None:
    assert set(JournalEntry.model_fields) == {
        "id", "profile", "changeset_id", "run_id", "accepted", "state", "intended",
        "effects", "write_failures", "conflicts", "error", "uncertain", "undo_of",
        "undone_by", "created_at", "completed_at",
    }  # fmt: skip
    e1 = lib.rename()
    undo_entry(e1.id, lib.ctx())
    assert len(lib.journal.entries) == 2  # undo adds an entry; nothing is removed
