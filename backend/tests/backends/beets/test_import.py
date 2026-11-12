from __future__ import annotations

from pathlib import Path

from rifferr.backends.base import RUN_MARKER, ImportDecision
from rifferr.backends.beets import BeetsBackend
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BeetsEnv


def run_import(backend: BeetsBackend, path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    return backend.import_folder(path, "quiet", None, run_id="01RUN", **kwargs)  # type: ignore[arg-type]


def test_BEETS_IMPORT_4_quiet_import_of_tagged_album(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    inbox = beets_env.inbox_album()
    result = run_import(backend, inbox)
    [album] = result.imported
    assert album.fields["album"] == "Fixture Album"
    assert album.fields["albumartist"] == "Fixture Artist"
    assert [i.fields["title"] for i in album.items] == ["First", "Second"]
    assert album.path is not None
    assert album.path.startswith(str(beets_env.music))  # copied into the library
    assert sorted(p.name for p in inbox.iterdir()) == ["01 First.flac", "02 Second.flac"]
    assert result.skipped == []


def test_BEETS_IMPORT_4_options_override_for_this_import_only(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    inbox = beets_env.inbox_album()
    run_import(backend, inbox, options={"move": True})
    assert not any(inbox.glob("*.flac"))  # moved
    second = beets_env.inbox_album(name="again")
    import beets

    assert beets.config["import"]["move"].get() is False
    assert run_import(backend, second, options={"duplicate_action": "keep"}).imported


def test_BEETS_IMPORT_5_imported_albums_and_items_get_the_run_marker(
    beets_env: BeetsEnv,
) -> None:
    backend = beets_env.backend()
    run_import(backend, beets_env.inbox_album())
    [album] = backend.query(Selection(run_id="01RUN"))
    assert album.fields[RUN_MARKER] == "01RUN"
    assert all(i.fields[RUN_MARKER] == "01RUN" for i in album.items)
    assert backend.query(Selection(run_id="01OTHER")) == []


def test_BEETS_IMPORT_6_decisions_are_reported(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    seen: list[ImportDecision] = []
    run_import(backend, beets_env.inbox_album(), on_decision=seen.append)
    assert [d.outcome for d in seen] == ["imported"]


def test_BEETS_IMPORT_7_duplicate_is_skipped_not_failed(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    run_import(backend, beets_env.inbox_album())
    seen: list[ImportDecision] = []
    result = run_import(backend, beets_env.inbox_album(name="copy"), on_decision=seen.append)
    assert result.imported == []
    assert [s.reason for s in result.skipped] == ["duplicate of an album in the library"]
    assert [d.reason for d in result.duplicates] == ["skip"]
    assert [d.outcome for d in seen] == ["duplicate", "skipped"]


def test_BEETS_IMPORT_7_incremental_skip_is_reported(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    inbox = beets_env.inbox_album()
    run_import(backend, inbox, options={"incremental": True})
    result = run_import(backend, inbox, options={"incremental": True})
    assert [s.reason for s in result.skipped] == ["already imported (incremental)"]


def test_BEETS_IMPORT_13_set_fields_are_plain_strings(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    value = "a$b %upper{c}, ${album}"
    result = run_import(backend, beets_env.inbox_album(), set_fields={"source": value})
    [album] = result.imported
    assert album.fields["source"] == value
    assert all(i.fields["source"] == value for i in album.items)


def test_BEETS_IMPORT_16_should_stop_ends_between_albums(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    beets_env.inbox_album(name="a")
    beets_env.inbox_album(fixture="single-mp3", name="b")
    calls = 0

    def stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    result = run_import(backend, beets_env.inbox, should_stop=stop)
    assert len(result.imported) == 1


def test_BEETS_IMPORT_16_no_stop_imports_everything(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    beets_env.inbox_album(name="a")
    beets_env.inbox_album(fixture="single-mp3", name="b")
    assert len(run_import(backend, beets_env.inbox).imported) == 2


def test_BEETS_IMPORT_14_library_is_not_held_between_calls(beets_env: BeetsEnv) -> None:
    import sqlite3

    backend = beets_env.backend()
    run_import(backend, beets_env.inbox_album())
    backend.query(Selection())
    # Another process (a `beet` subprocess) can take the write lock right away.
    conn = sqlite3.connect(beets_env.beetsdir / "library.db", timeout=0)
    conn.execute("BEGIN EXCLUSIVE")
    conn.rollback()
    conn.close()
