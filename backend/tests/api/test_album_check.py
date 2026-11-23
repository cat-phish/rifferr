"""LIBRARY-MOVE-18: before-run values for a FileChangeSet's album check."""

from __future__ import annotations

from rifferr.appdb import AppChangeSets, AppImportDecisions, AppJournal
from rifferr.core.changeset import (
    ApplyResult,
    ChangeSet,
    FieldChange,
    FieldEffect,
    FileEffect,
    JournalEntry,
    TargetChange,
    TargetRef,
)
from tests.api.conftest import Api
from tests.api.test_imports_api import decision
from tests.api.test_review_queue import SOURCE, T0, waiting_run

ALBUM = TargetRef(scope="album", id="a1")
ITEM = TargetRef(scope="item", id="i1")


def move(run_id: str | None, album: TargetRef | None = ALBUM) -> ChangeSet:
    return ChangeSet(
        id="mv",
        profile="main",
        run_id=run_id,
        step_id="into-library",
        source=SOURCE,
        created_at=T0,
        targets=[],
        files=[
            FileEffect(
                target=ITEM,
                kind="item",
                src="/staging/1.flac",
                dst="/music/1.flac",
                mode="move",
                size=1,
                sha256="0" * 64,
            )
        ],
        album=album,
    )


def applied(api: Api, entry_id: str, run_id: str, effects: list[FieldEffect]) -> None:
    journal = AppJournal(api.state.db)
    change = FieldChange(field="x", old=None, new=1)
    journal.begin(
        JournalEntry(
            id=entry_id,
            profile="main",
            changeset_id=f"cs-{entry_id}",
            run_id=run_id,
            intended=[TargetChange(target=ALBUM, changes=[change])],
            created_at=T0,
        )
    )
    journal.complete(entry_id, ApplyResult(applied=True, effects=effects))


async def test_LIBRARY_MOVE_18_before_values_from_journal_and_import(api: Api) -> None:
    db = api.state.db
    run = waiting_run(db, holds=["mv"]).run
    AppChangeSets(db).add(move(run))

    def eff(target: TargetRef, field: str, before: object, after: object) -> FieldEffect:
        return FieldEffect(target=target, field=field, before=before, after=after)  # type: ignore[arg-type]

    applied(api, "j1", run, [eff(ALBUM, "album", "A / B", "A + B"), eff(ITEM, "title", "t", "T")])
    # A later change keeps the first before value.
    applied(api, "j2", run, [eff(ALBUM, "album", "A + B", "A + B (EP)")])
    applied(api, "j3", run, [eff(TargetRef(scope="album", id="other"), "album", "x", "y")])
    imported = decision("d1", run)
    imported.album_id = "a1"
    imported.state = "applied"
    AppImportDecisions(db).add(imported)

    body = (await api.client.get("/api/changesets/mv/before")).json()
    assert body["album_id"] == "a1"
    assert body["album"] == {"album": "A / B"}
    assert body["items"] == {"i1": {"title": "t"}}
    assert body["folder"][0]["tags"] == {"title": "One"}


async def test_LIBRARY_MOVE_18_before_without_album_or_run(api: Api) -> None:
    AppChangeSets(api.state.db).add(move(None, album=None))
    body = (await api.client.get("/api/changesets/mv/before")).json()
    assert body == {"album_id": None, "album": {}, "items": {}, "folder": []}
    r = await api.client.get("/api/changesets/nope/before")
    assert r.status_code == 404
