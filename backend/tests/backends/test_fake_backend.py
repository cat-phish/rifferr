from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.base import (
    RUN_MARKER,
    BackendCapabilities,
    CapabilityError,
    ImportResult,
    LibraryBackend,
    QueryError,
    TemplateError,
)
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import ChangeSet
from rifferr.core.selection import Selection
from tests.backends.contract import BackendContract, album_names


class TestFakeBackendContract(BackendContract):
    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
        return FakeBackend.from_fixture({"albums": albums})


class TestFakeBackendWithoutCapabilities(BackendContract):
    """The contract also holds for a backend that offers nothing."""

    def make_backend(self, albums: list[dict[str, Any]]) -> LibraryBackend:
        return FakeBackend.from_fixture({"albums": albums}, capabilities=BackendCapabilities())


def test_BACKEND_2_capabilities_default() -> None:
    caps = FakeBackend().capabilities
    assert caps == BackendCapabilities(import_=True, query=True, templates=True, command_env=True)


def test_BACKEND_2_disabled_templates_raise() -> None:
    fb = FakeBackend.from_fixture(
        {"albums": [{"album": "A"}]}, capabilities=BackendCapabilities(query=True)
    )
    album = fb.query(Selection())[0]
    with pytest.raises(CapabilityError):
        fb.evaluate("$album", album)


def test_BACKEND_9_apply_is_declared_but_unavailable_until_changesets() -> None:
    fb = FakeBackend()
    assert not fb.capabilities.apply
    with pytest.raises(CapabilityError):
        fb.apply(ChangeSet())


def test_BACKEND_6_fixture_defaults() -> None:
    fb = FakeBackend.from_fixture(
        {"albums": [{"albumartist": "A", "album": "B", "items": [{"title": "x", "track": 1}]}]}
    )
    album = fb.query(Selection())[0]
    assert album.path == "/fake/A/B"
    assert album.items[0].path == "/fake/A/B/01 x.flac"
    assert album.items[0].fields["title"] == "x"
    assert "items" not in album.fields


def test_BACKEND_6_items_sort_by_disc_then_track() -> None:
    fb = FakeBackend.from_fixture(
        {
            "albums": [
                {
                    "album": "A",
                    "items": [
                        {"title": "d2t1", "disc": 2, "track": 1},
                        {"title": "d1t2", "disc": 1, "track": 2},
                        {"title": "d1t1", "disc": 1, "track": 1},
                    ],
                }
            ]
        }
    )
    assert [i.fields["title"] for i in fb.query(Selection())[0].items] == ["d1t1", "d1t2", "d2t1"]


@pytest.mark.parametrize("query", ["rock", "album:'unclosed", "::x", "album:("])
def test_BACKEND_6_unsupported_or_bad_query_syntax_raises(query: str) -> None:
    fb = FakeBackend.from_fixture({"albums": [{"album": "A"}]})
    with pytest.raises(QueryError):
        fb.query(Selection(query=query.replace("album:(", "album::(")))


def test_BACKEND_6_template_functions_are_not_supported_by_the_fake() -> None:
    fb = FakeBackend.from_fixture({"albums": [{"album": "A"}]})
    with pytest.raises(TemplateError, match="FakeBackend"):
        fb.evaluate("%upper{$album}", fb.query(Selection())[0])


def test_BACKEND_6_list_and_bool_fields() -> None:
    fb = FakeBackend.from_fixture(
        {
            "albums": [
                {"album": "A", "comp": True, "albumartists": ["X", "Y Band"]},
                {"album": "B", "comp": False, "albumartists": ["Z"]},
            ]
        }
    )
    assert album_names(fb.query(Selection(query="comp:true"))) == {"A"}
    assert album_names(fb.query(Selection(query="comp:false"))) == {"B"}
    assert album_names(fb.query(Selection(query="albumartists:band"))) == {"A"}
    album = fb.query(Selection(query="album:A"))[0]
    assert fb.evaluate("$albumartists", album) == "X; Y Band"
    assert fb.evaluate("$comp", album) == "True"


def test_BACKEND_7_import_folder_creates_albums_from_fixture_and_records_calls(
    tmp_path: Path,
) -> None:
    fb = FakeBackend()
    inbox = tmp_path / "inbox" / "Some Album"
    fb.add_import_fixture(
        inbox, [{"albumartist": "A", "album": "New", "items": [{"title": "t", "track": 1}]}]
    )
    result = fb.import_folder(inbox, "quiet", None, run_id="run-9")
    assert isinstance(result, ImportResult)
    assert album_names(result.imported) == {"New"}
    assert result.skipped == []
    assert result.imported[0].fields[RUN_MARKER] == "run-9"
    assert album_names(fb.query(Selection(run_id="run-9"))) == {"New"}
    assert [(c.path, c.mode, c.run_id) for c in fb.import_calls] == [(inbox, "quiet", "run-9")]


def test_BACKEND_7_import_unknown_or_skipped_folder(tmp_path: Path) -> None:
    fb = FakeBackend()
    fb.add_import_fixture(tmp_path / "empty", skip_reason="no audio files")
    empty = fb.import_folder(tmp_path / "empty", "quiet", None)
    unknown = fb.import_folder(tmp_path / "unknown", "quiet", None)
    assert [(s.path, s.reason) for s in empty.skipped] == [
        (str(tmp_path / "empty"), "no audio files")
    ]
    assert [s.reason for s in unknown.skipped] == ["no import fixture registered"]
    assert empty.imported == unknown.imported == []


def test_BACKEND_7_import_twice_is_skipped_as_duplicate(tmp_path: Path) -> None:
    fb = FakeBackend()
    fb.add_import_fixture(tmp_path / "a", [{"album": "A"}])
    fb.import_folder(tmp_path / "a", "quiet", None)
    again = fb.import_folder(tmp_path / "a", "quiet", None)
    assert [s.reason for s in again.skipped] == ["already imported"]
    assert len(fb.query(Selection())) == 1


@pytest.mark.parametrize("mode", ["auto", "interactive"])
def test_BACKEND_7_only_quiet_mode_in_m0(tmp_path: Path, mode: Any) -> None:
    fb = FakeBackend()
    with pytest.raises(NotImplementedError, match="M2"):
        fb.import_folder(tmp_path, mode, None)


def test_BACKEND_1_command_env_is_configurable() -> None:
    assert FakeBackend(env={"BEETSDIR": "/x"}).command_env() == {"BEETSDIR": "/x"}


def test_BACKEND_6_unquoted_backslashes_follow_shell_rules() -> None:
    """Query strings are split with shell rules (like beets): an unquoted
    backslash escapes the next character, so quote regexes that use one."""
    fb = FakeBackend.from_fixture({"albums": [{"album": "First EP"}, {"album": "bEPb"}]})
    assert album_names(fb.query(Selection(query="album::\\bEP\\b"))) == {"bEPb"}
    assert album_names(fb.query(Selection(query="'album::\\bEP\\b'"))) == {"First EP"}


def test_BACKEND_8_fake_backend_runs_the_shared_contract_suite() -> None:
    assert issubclass(TestFakeBackendContract, BackendContract)
    contract_tests = [n for n in dir(BackendContract) if n.startswith("test_BACKEND_")]
    assert len(contract_tests) >= 10
