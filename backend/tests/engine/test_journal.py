from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rifferr.appdb import AppDB, AppJournal
from rifferr.core.changeset import (
    ApplyResult,
    Conflict,
    FieldChange,
    FieldEffect,
    JournalEntry,
    TargetChange,
    TargetRef,
    WriteFailure,
)
from rifferr.engine import InMemoryJournal, Journal, JournalError

T0 = datetime(2026, 11, 12, 12, tzinfo=UTC)
ALBUM = TargetRef(scope="album", id="album-1")


def entry(id: str = "j1", run_id: str | None = "run-1", offset: int = 0) -> JournalEntry:
    return JournalEntry(
        id=id,
        profile="main",
        changeset_id=f"cs-{id}",
        run_id=run_id,
        intended=[
            TargetChange(target=ALBUM, changes=[FieldChange(field="album", old="A", new="B")])
        ],
        created_at=T0 + timedelta(seconds=offset),
    )


@pytest.fixture(params=["memory", "appdb"])
def journal(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Journal]:
    if request.param == "memory":
        yield InMemoryJournal()
        return
    db = AppDB(tmp_path / "app.db")
    yield AppJournal(db)
    db.close()


def test_JOURNAL_1_begin_records_a_pending_entry_with_the_intent(journal: Journal) -> None:
    journal.begin(entry())
    (pending,) = journal.pending("main")
    assert pending.state == "pending"
    assert pending.intended[0].changes[0] == FieldChange(field="album", old="A", new="B")
    assert journal.pending("other") == []


def test_JOURNAL_1_begin_is_durable_across_connections(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "app.db")
    AppJournal(db).begin(entry())
    other = AppDB(tmp_path / "app.db")
    assert [e.id for e in AppJournal(other).pending("main")] == ["j1"]
    db.close()
    other.close()


def test_JOURNAL_1_entries_are_never_begun_twice(journal: Journal) -> None:
    journal.begin(entry())
    with pytest.raises(JournalError):
        journal.begin(entry())


def test_JOURNAL_2_complete_applied_records_effects_and_write_failures(
    journal: Journal,
) -> None:
    journal.begin(entry())
    item = TargetRef(scope="item", id="item-1")
    result = ApplyResult(
        applied=True,
        effects=[
            FieldEffect(target=ALBUM, field="album", before="A", after="B"),
            FieldEffect(target=item, field="album", before="A", after="B"),
        ],
        write_failures=[WriteFailure(target=item, path="/m/1.flac", error="read-only")],
    )
    done = journal.complete("j1", result)
    assert done.state == "applied"
    assert done.completed_at is not None
    assert journal.get("j1") == done
    assert done.effects == result.effects
    assert done.write_failures == result.write_failures
    assert journal.pending("main") == []


def test_JOURNAL_2_complete_failed_records_conflicts(journal: Journal) -> None:
    journal.begin(entry())
    conflict = Conflict(target=ALBUM, field="album", expected="A", current="A!")
    done = journal.complete("j1", ApplyResult(applied=False, conflicts=[conflict]))
    assert done.state == "failed"
    assert done.conflicts == [conflict]
    assert done.effects == []


def test_JOURNAL_2_fail_records_the_error(journal: Journal) -> None:
    journal.begin(entry())
    assert journal.fail("j1", "library locked").error == "library locked"


def test_JOURNAL_2_completed_entries_are_not_changed_again(journal: Journal) -> None:
    journal.begin(entry())
    journal.complete("j1", ApplyResult(applied=True))
    with pytest.raises(JournalError):
        journal.complete("j1", ApplyResult(applied=True))
    with pytest.raises(JournalError):
        journal.fail("j1", "late")
    with pytest.raises(JournalError):
        journal.complete("missing", ApplyResult(applied=True))


def test_JOURNAL_2_only_undone_by_may_change_later(journal: Journal) -> None:
    journal.begin(entry())
    journal.complete("j1", ApplyResult(applied=True))
    journal.mark_undone("j1", "j2")
    got = journal.get("j1")
    assert got is not None
    assert got.undone_by == "j2"
    with pytest.raises(JournalError):
        journal.mark_undone("j1", "j3")


def test_JOURNAL_8_for_run_lists_entries_oldest_first(journal: Journal) -> None:
    journal.begin(entry("j2", offset=5))
    journal.begin(entry("j1", offset=0))
    journal.begin(entry("j3", run_id="run-2"))
    assert [e.id for e in journal.for_run("run-1")] == ["j1", "j2"]
