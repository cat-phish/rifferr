"""Backend contract suite (BACKEND-8).

Every LibraryBackend implementation runs these tests against itself by
subclassing ``BackendContract`` and implementing ``make_backend``. The suite
pins the behaviors the engine and steps rely on, in beets semantics, for the
query/template subset documented in C-BACKEND.

Tests for a capability the backend doesn't offer are skipped; the backend must
then raise ``CapabilityError`` instead (checked by ``test_BACKEND_2_...``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar

import pytest

from rifferr.backends.base import (
    RUN_MARKER,
    BackendCapabilities,
    CapabilityError,
    ChangeSetRejected,
    LibraryBackend,
)
from rifferr.core.changeset import (
    ChangeSet,
    ChangeSource,
    FieldChange,
    FieldEffect,
    TargetChange,
    TargetRef,
)
from rifferr.core.selection import Selection
from rifferr.core.snapshot import AlbumSnapshot

#: The standard library every backend is seeded with. Field values use beets
#: field names and types. ``rifferr_run`` is the run marker flexible attribute.
STANDARD_LIBRARY: list[dict[str, Any]] = [
    {
        "albumartist": "Fixture Artist",
        "album": "First EP",
        "albumtype": "ep",
        "year": 2001,
        "genres": ["Rock"],
        RUN_MARKER: "run-1",
        "items": [
            {"title": "Two", "track": 2, "artist": "Fixture Artist"},
            {"title": "One", "track": 1, "artist": "Fixture Artist"},
        ],
    },
    {
        "albumartist": "Other Artist",
        "album": "Long Player",
        "albumtype": "album",
        "year": 2010,
        "genres": ["Jazz"],
        "items": [{"title": "Solo feat. Guest", "track": 1, "artist": "Other Artist"}],
    },
    {
        "albumartist": "Fixture Artist",
        "album": "Second (EP)",
        "albumtype": "ep",
        "year": 2005,
        "genres": ["Rock"],
        RUN_MARKER: "run-2",
        "items": [{"title": "Three", "track": 1, "artist": "Fixture Artist"}],
    },
]


def changeset(*targets: tuple[TargetRef, list[FieldChange]]) -> ChangeSet:
    return ChangeSet(
        id="cs-contract",
        profile="test",
        source=ChangeSource(kind="manual", name="contract"),
        created_at=datetime(2026, 11, 12, tzinfo=UTC),
        targets=[TargetChange(target=t, changes=c) for t, c in targets],
    )


def album_ref(album: AlbumSnapshot) -> TargetRef:
    return TargetRef(scope="album", id=album.id)


def item_ref(album: AlbumSnapshot, index: int) -> TargetRef:
    return TargetRef(scope="item", id=album.items[index].id)


def effect_keys(effects: list[FieldEffect]) -> set[tuple[str, str]]:
    return {(e.target.id, e.field) for e in effects}


def album_names(albums: list[AlbumSnapshot]) -> set[str]:
    return {str(a.fields["album"]) for a in albums}


class BackendContract:
    """Subclass, set ``capabilities_under_test`` if needed, implement ``make_backend``."""

    library: ClassVar[list[dict[str, Any]]] = STANDARD_LIBRARY

    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
        raise NotImplementedError

    def read_only(self, backend: LibraryBackend) -> LibraryBackend:
        """The same library opened read-only (BACKEND-13)."""
        raise NotImplementedError

    @pytest.fixture
    def backend(self) -> LibraryBackend:
        return self.make_backend(self.library)

    def need(self, backend: LibraryBackend, capability: str) -> None:
        if not getattr(backend.capabilities, capability):
            pytest.skip(f"backend lacks capability {capability}")

    def by_album(self, backend: LibraryBackend, name: str) -> AlbumSnapshot:
        self.need(backend, "query")
        found = [a for a in backend.query(Selection()) if a.fields["album"] == name]
        assert len(found) == 1, name
        return found[0]

    # -- protocol shape ------------------------------------------------------

    def test_BACKEND_1_implements_protocol(self, backend: LibraryBackend) -> None:
        assert isinstance(backend, LibraryBackend)
        assert isinstance(backend.capabilities, BackendCapabilities)

    def test_BACKEND_2_unsupported_methods_raise_capability_error(
        self, backend: LibraryBackend, tmp_path: Any
    ) -> None:
        caps = backend.capabilities
        calls: dict[str, Any] = {
            "query": lambda: backend.query(Selection()),
            "command_env": backend.command_env,
            "import_": lambda: backend.import_folder(tmp_path, "quiet", None),
            "apply": lambda: backend.apply(
                changeset((TargetRef(scope="item", id="x"), [FieldChange(field="t", new="x")]))
            ),
        }
        for cap, call in calls.items():
            if not getattr(caps, cap):
                with pytest.raises(CapabilityError):
                    call()

    # -- snapshots -----------------------------------------------------------

    def test_BACKEND_3_snapshots_have_ids_paths_and_fields(self, backend: LibraryBackend) -> None:
        self.need(backend, "query")
        albums = backend.query(Selection())
        assert album_names(albums) == {"First EP", "Long Player", "Second (EP)"}
        ids = [a.id for a in albums] + [i.id for a in albums for i in a.items]
        assert len(ids) == len(set(ids)), "ids must be unique"
        for album in albums:
            assert album.fields["albumartist"]
            for item in album.items:
                assert item.path
                assert item.fields["title"]

    def test_BACKEND_4_items_in_track_order(self, backend: LibraryBackend) -> None:
        first = self.by_album(backend, "First EP")
        assert [i.fields["title"] for i in first.items] == ["One", "Two"]

    def test_BACKEND_4_snapshots_round_trip_through_json(self, backend: LibraryBackend) -> None:
        self.need(backend, "query")
        for album in backend.query(Selection()):
            assert AlbumSnapshot.model_validate_json(album.model_dump_json()) == album

    def test_BACKEND_3_field_types_follow_beets(self, backend: LibraryBackend) -> None:
        album = self.by_album(backend, "First EP")
        assert album.fields["year"] == 2001
        assert album.items[0].fields["track"] == 1
        assert album.fields[RUN_MARKER] == "run-1"

    # -- selection / query ---------------------------------------------------

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("", {"First EP", "Long Player", "Second (EP)"}),
            ("albumtype:ep", {"First EP", "Second (EP)"}),
            ("album:ep", {"First EP", "Second (EP)"}),  # substring, case-insensitive
            ("album:FIRST", {"First EP"}),
            ("album::^Second", {"Second (EP)"}),  # regex
            ("album::ep", set()),  # regex is case-sensitive
            ("'album::\\bEP\\b'", {"First EP", "Second (EP)"}),  # quoted: keeps backslashes
            ("^albumtype:ep", {"Long Player"}),  # negation
            ("albumtype:ep year:2005", {"Second (EP)"}),  # AND
            ("albumtype:ep '^album::\\bEP\\b'", set()),
            ("year:2001", {"First EP"}),  # numeric: exact
            ("year:200", set()),
            ("'album:First EP'", {"First EP"}),  # quoted term with a space
            ("nosuchfield:x", set()),  # missing flexattr never matches
            ("genres:rock", {"First EP", "Second (EP)"}),
        ],
    )
    def test_BACKEND_6_album_queries(
        self, backend: LibraryBackend, query: str, expected: set[str]
    ) -> None:
        self.need(backend, "query")
        assert album_names(backend.query(Selection(query=query))) == expected

    def test_BACKEND_5_item_scope_returns_albums_with_matching_items(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "query")
        albums = backend.query(Selection(query="title:one", scope="item"))
        assert album_names(albums) == {"First EP"}
        assert [i.fields["title"] for i in albums[0].items] == ["One"]

    def test_BACKEND_5_run_id_restricts_to_albums_touched_by_run(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "query")
        assert album_names(backend.query(Selection(run_id="run-1"))) == {"First EP"}
        assert album_names(backend.query(Selection(run_id="run-2", query="genres:rock"))) == {
            "Second (EP)"
        }
        assert backend.query(Selection(run_id="run-1", query="albumtype:album")) == []
        assert backend.query(Selection(run_id="nope")) == []

    def test_BACKEND_6_matches_album_and_item(self, backend: LibraryBackend) -> None:
        album = self.by_album(backend, "First EP")
        assert backend.matches("albumtype:ep", album)
        assert not backend.matches("^albumtype:ep", album)
        assert backend.matches("title:one", album.items[0])
        assert not backend.matches("title:one", album.items[1])
        assert backend.matches("", album)

    # -- templates -----------------------------------------------------------

    @pytest.mark.parametrize(
        ("template", "expected"),
        [
            ("$albumartist - $album", "Fixture Artist - First EP"),
            ("${album}!", "First EP!"),
            ("$nosuchfield", "$nosuchfield"),  # unknown fields stay literal
            ("100$$", "100$"),
            ("plain text", "plain text"),
            ("$genres/$albumtype", "Rock/ep"),
        ],
    )
    def test_BACKEND_6_evaluate_album_templates(
        self, backend: LibraryBackend, template: str, expected: str
    ) -> None:
        self.need(backend, "templates")
        assert backend.evaluate(template, self.by_album(backend, "First EP")) == expected

    def test_BACKEND_6_evaluate_item_sees_album_fields(self, backend: LibraryBackend) -> None:
        self.need(backend, "templates")
        item = self.by_album(backend, "First EP").items[0]
        assert backend.evaluate("$albumartist: $title", item) == "Fixture Artist: One"

    def test_BACKEND_6_evaluate_path(self, backend: LibraryBackend) -> None:
        self.need(backend, "templates")
        album = self.by_album(backend, "First EP")
        assert backend.evaluate("$path", album.items[0]) == album.items[0].path

    # -- environment ---------------------------------------------------------

    def test_BACKEND_1_command_env_is_string_mapping(self, backend: LibraryBackend) -> None:
        self.need(backend, "command_env")
        env = backend.command_env()
        assert isinstance(env, Mapping)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())

    # -- ChangeSets (C-CHANGESET) ---------------------------------------------

    def test_CHANGESET_10_album_change_is_stored_and_carried_to_items(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        change = FieldChange(field="album", old="First EP", new="First EP (Live)")
        result = backend.apply(changeset((album_ref(ep), [change])))
        assert result.applied
        assert not result.conflicts
        after = self.by_album(backend, "First EP (Live)")
        assert [i.fields["album"] for i in after.items] == ["First EP (Live)"] * 2
        assert self.by_album(backend, "Long Player").items[0].fields["album"] == "Long Player"

    def test_CHANGESET_11_effects_include_propagated_item_fields(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        change = FieldChange(field="year", old=2001, new=2002)
        result = backend.apply(changeset((album_ref(ep), [change])))
        assert effect_keys(result.effects) == {
            (ep.id, "year"),
            (ep.items[0].id, "year"),
            (ep.items[1].id, "year"),
        }
        assert all(e.before == 2001 and e.after == 2002 for e in result.effects)

    def test_CHANGESET_10_item_change_stays_on_the_item(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        change = FieldChange(field="title", old="One", new="One (Intro)")
        result = backend.apply(changeset((item_ref(ep, 0), [change])))
        assert effect_keys(result.effects) == {(ep.items[0].id, "title")}
        after = self.by_album(backend, "First EP")
        assert [i.fields["title"] for i in after.items] == ["One (Intro)", "Two"]

    def test_CHANGESET_11_new_field_records_that_it_did_not_exist(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        result = backend.apply(
            changeset((item_ref(ep, 1), [FieldChange(field="mood", old=None, new="calm")]))
        )
        (effect,) = result.effects
        assert (effect.before, effect.after, effect.existed_before) == (None, "calm", False)
        assert self.by_album(backend, "First EP").items[1].fields["mood"] == "calm"

    def test_CHANGESET_10_delete_removes_a_flexible_attribute(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        add = FieldChange(field="mood", old=None, new="calm")
        backend.apply(changeset((item_ref(ep, 0), [add])))
        remove = FieldChange(field="mood", op="delete", old="calm")
        result = backend.apply(changeset((item_ref(ep, 0), [remove])))
        (effect,) = result.effects
        assert (effect.before, effect.after, effect.existed_before) == ("calm", None, True)
        assert "mood" not in self.by_album(backend, "First EP").items[0].fields

    def test_CHANGESET_8_conflict_applies_nothing(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        good = FieldChange(field="year", old=2001, new=2003)
        stale = FieldChange(field="title", old="Not One", new="X")
        result = backend.apply(changeset((album_ref(ep), [good]), (item_ref(ep, 0), [stale])))
        assert not result.applied
        assert not result.effects
        (conflict,) = result.conflicts
        assert (conflict.field, conflict.expected, conflict.current) == ("title", "Not One", "One")
        after = self.by_album(backend, "First EP")
        assert after.fields["year"] == 2001
        assert after.items[0].fields["title"] == "One"

    def test_CHANGESET_8_old_none_conflicts_with_an_existing_value(
        self, backend: LibraryBackend
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        result = backend.apply(
            changeset((album_ref(ep), [FieldChange(field="albumtype", old=None, new="live")]))
        )
        assert not result.applied
        assert result.conflicts[0].current == "ep"

    @pytest.mark.parametrize("field", ["id", "path", "album_id", "mtime", "added"])
    def test_CHANGESET_2_read_only_fields_are_rejected(
        self, backend: LibraryBackend, field: str
    ) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        cs = changeset((item_ref(ep, 0), [FieldChange(field=field, new="x")]))
        (problem,) = backend.check(cs)
        assert problem.field == field
        with pytest.raises(ChangeSetRejected):
            backend.apply(cs)

    def test_CHANGESET_2_values_must_fit_the_field_type(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        bad = changeset((album_ref(ep), [FieldChange(field="year", old=2001, new="soon")]))
        assert [p.field for p in backend.check(bad)] == ["year"]
        with pytest.raises(ChangeSetRejected):
            backend.apply(bad)
        assert self.by_album(backend, "First EP").fields["year"] == 2001
        good = changeset((album_ref(ep), [FieldChange(field="year", old=2001, new=1999)]))
        assert backend.check(good) == []

    def test_CHANGESET_2_unknown_targets_are_problems(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        missing = TargetRef(scope="item", id=ep.id + "-gone")
        problems = backend.check(changeset((missing, [FieldChange(field="title", new="x")])))
        assert [p.target for p in problems] == [missing]

    def test_CHANGESET_12_list_fields_round_trip(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ep = self.by_album(backend, "First EP")
        change = FieldChange(field="genres", old=["Rock"], new=["Rock", "Live"])
        assert backend.apply(changeset((album_ref(ep), [change]))).applied
        after = self.by_album(backend, "First EP")
        assert after.fields["genres"] == ["Rock", "Live"]
        assert after.items[0].fields["genres"] == ["Rock", "Live"]

    # -- working values, types, shared fields, read-only (S-TAGSCRIPT) ------------

    def test_BACKEND_10_evaluate_uses_snapshot_values(self, backend: LibraryBackend) -> None:
        self.need(backend, "templates")
        ep = self.by_album(backend, "First EP")
        changed = ep.model_copy(update={"fields": {**ep.fields, "album": "Renamed"}})
        assert backend.evaluate("$album ($year)", changed) == "Renamed (2001)"
        item = ep.items[0]
        working = item.model_copy(update={"fields": {**item.fields, "title": "Uno"}})
        assert backend.evaluate("$title", working) == "Uno"
        # not stored: the library still has the old values
        assert backend.evaluate("$album", ep) == "First EP"

    def test_BACKEND_10_absent_fields_fall_back_to_stored(self, backend: LibraryBackend) -> None:
        self.need(backend, "templates")
        ep = self.by_album(backend, "First EP")
        partial = ep.model_copy(update={"fields": {"album": "Partial"}})
        assert backend.evaluate("$album $year", partial) == "Partial 2001"

    def test_BACKEND_10_none_removes_a_flexible_attribute(self, backend: LibraryBackend) -> None:
        self.need(backend, "query")
        ep = self.by_album(backend, "First EP")
        assert backend.matches(f"{RUN_MARKER}:run-1", ep)
        gone = ep.model_copy(update={"fields": {**ep.fields, RUN_MARKER: None}})
        assert not backend.matches(f"{RUN_MARKER}:run-1", gone)

    def test_BACKEND_10_matches_uses_snapshot_values(self, backend: LibraryBackend) -> None:
        self.need(backend, "query")
        ep = self.by_album(backend, "First EP")
        assert not backend.matches("album::[(]EP[)]$", ep)
        renamed = ep.model_copy(update={"fields": {**ep.fields, "album": "First (EP)"}})
        assert backend.matches("album::[(]EP[)]$", renamed)
        assert backend.matches(
            "year:1999", renamed.model_copy(update={"fields": {**renamed.fields, "year": 1999}})
        )

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("year", "1999", 1999),
            ("year", " 2001 ", 2001),
            ("year", 2003, 2003),
            ("year", "", 0),
            ("comp", "yes", True),
            ("comp", "false", False),
            ("comp", True, True),
            ("title", "x", "x"),
            ("title", 12, "12"),
            ("genres", "Rock; Jazz", ["Rock", "Jazz"]),
            ("genres", ["Rock"], ["Rock"]),
            ("genres", "", []),
            ("rifferr_flex", "7", "7"),
        ],
    )
    def test_BACKEND_11_coerce_converts_to_field_type(
        self, backend: LibraryBackend, field: str, value: Any, expected: Any
    ) -> None:
        result = backend.coerce(field, value)
        assert result == expected
        assert type(result) is type(expected)

    @pytest.mark.parametrize(
        ("field", "value"), [("year", "soon"), ("year", "19.5"), ("comp", "maybe"), ("year", True)]
    )
    def test_BACKEND_11_coerce_raises_value_error(
        self, backend: LibraryBackend, field: str, value: Any
    ) -> None:
        with pytest.raises(ValueError, match=r"expects"):
            backend.coerce(field, value)

    def test_BACKEND_12_album_item_fields(self, backend: LibraryBackend) -> None:
        shared = backend.album_item_fields()
        for name in ("album", "albumartist", "year", "genres", RUN_MARKER):
            assert name in shared
        assert "id" not in shared

    def test_BACKEND_13_read_only_refuses_writes(self, backend: LibraryBackend) -> None:
        self.need(backend, "apply")
        ro = self.read_only(backend)
        assert not ro.capabilities.apply
        assert not ro.capabilities.import_
        ep = self.by_album(ro, "First EP")
        assert ro.evaluate("$album", ep) == "First EP"
        cs = changeset((album_ref(ep), [FieldChange(field="album", old="First EP", new="X")]))
        with pytest.raises(CapabilityError):
            ro.apply(cs)
        assert self.by_album(backend, "First EP").fields["album"] == "First EP"

    def test_TAG_GRID_7_read_only_backend_checks_grid_edits(self, backend: LibraryBackend) -> None:
        """The tag grid browses and checks edits in the read-only worker (TAG-GRID-2, -7)."""
        from rifferr.engine.manual import (
            LibraryQuery,
            ManualChange,
            ManualChanges,
            check_manual,
            library_page,
        )

        self.need(backend, "apply")
        ro = self.read_only(backend)
        page = library_page(ro, LibraryQuery(query="albumtype:ep"))
        assert sorted(str(a.fields["album"]) for a in page.albums) == ["First EP", "Second (EP)"]
        assert {"album", "year", "genres"} <= set(page.album_fields)
        assert {"title", "track"} <= set(page.item_fields)
        ep = self.by_album(ro, "First EP")
        edit = ManualChange(
            target=album_ref(ep),
            album=ep.id,
            changes=[
                FieldChange(field="year", old=2001, new="2002"),
                FieldChange(field="genres", old=["Rock"], new=["Rock", "Live"]),
            ],
        )
        result = check_manual(ro, ManualChanges(targets=[edit]))
        assert result.problems == []
        assert [c.new for c in result.targets[0].changes] == [2002, ["Rock", "Live"]]
        bad = edit.model_copy(update={"changes": [FieldChange(field="path", new="/x")]})
        assert [p.field for p in check_manual(ro, ManualChanges(targets=[bad])).problems] == [
            "path"
        ]
        assert self.by_album(backend, "First EP").fields["year"] == 2001
