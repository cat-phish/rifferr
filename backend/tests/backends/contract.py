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
from typing import Any, ClassVar

import pytest

from rifferr.backends.base import (
    RUN_MARKER,
    BackendCapabilities,
    CapabilityError,
    LibraryBackend,
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


def album_names(albums: list[AlbumSnapshot]) -> set[str]:
    return {str(a.fields["album"]) for a in albums}


class BackendContract:
    """Subclass, set ``capabilities_under_test`` if needed, implement ``make_backend``."""

    library: ClassVar[list[dict[str, Any]]] = STANDARD_LIBRARY

    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
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
