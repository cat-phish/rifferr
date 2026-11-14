"""S-FILES-CLEANUP listing and cleaning, on folders under tmp_path."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rifferr.steps.cleanup_plan import (
    TRASH_MARKER,
    BusyFolder,
    CleanupError,
    Patterns,
    Plan,
    check_root,
    check_trash,
    execute,
    plan,
    purge_trash,
)

MUSIC = {".flac", ".mp3"}


def tree(root: Path, *files: str) -> Path:
    for f in files:
        p = root / f
        if f.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"content of {f}")
    return root


def make_plan(
    root: Path,
    *,
    delete: list[str] | None = None,
    keep: list[str] | None = None,
    empty_dirs: bool = True,
    remove_root: bool = False,
    busy: list[BusyFolder] | None = None,
    known: set[Path] | None = None,
    allow_audio: bool = False,
) -> Plan:
    return plan(
        root,
        delete=delete or [],
        keep=keep or [],
        empty_dirs=empty_dirs,
        remove_root=remove_root,
        busy=busy or [],
        library_files=lambda ps: {p for p in ps if p in (known or set())},
        importable=lambda ps: {p for p in ps if p.suffix.lower() in MUSIC},
        allow_audio=allow_audio,
    )


def classes(p: Plan) -> dict[str, str]:
    return {e.path: e.cls for e in p.entries}


# -- patterns -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "hit"),
    [
        ("*.nfo", "album.nfo", True),
        ("*.nfo", "CD1/album.NFO", True),  # no "/": any depth, any case
        ("*.nfo", "album.nfo.txt", False),
        ("Scans/*.jpg", "Scans/front.jpg", True),
        ("Scans/*.jpg", "CD1/Scans/front.jpg", False),
        ("**/*.sfv", "a/b/c.sfv", True),
        ("**/*.sfv", "c.sfv", True),
        ("Thumbs.db", "x/Thumbs.db", True),
        ("[!a]*.log", "rip.log", True),
        ("[!a]*.log", "aa.log", False),
        ("?.m3u", "a.m3u", True),
    ],
)
def test_FILES_CLEANUP_1_patterns(pattern: str, path: str, hit: bool) -> None:
    assert (Patterns([pattern]).match(path) is not None) is hit


# -- FILES-CLEANUP-3 guards ------------------------------------------------------------


def test_FILES_CLEANUP_3_root_guards(tmp_path: Path) -> None:
    lib = tmp_path / "music"
    lib.mkdir()
    with pytest.raises(CleanupError, match="absolute"):
        check_root(Path("relative"), [], None)
    with pytest.raises(CleanupError, match="not a folder"):
        check_root(tmp_path / "missing", [], None)
    with pytest.raises(CleanupError, match="file system root"):
        check_root(Path("/"), [], None)
    with pytest.raises(CleanupError, match="home"):
        check_root(tmp_path, [], tmp_path)
    with pytest.raises(CleanupError, match="library"):
        check_root(tmp_path, [lib], None)  # contains the library
    with pytest.raises(CleanupError, match="library"):
        check_root(lib, [lib], None)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    assert check_root(inbox, [lib], None) == inbox.resolve()


def test_FILES_CLEANUP_3_symlinked_root_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(CleanupError):
        check_root(tmp_path / "link", [], None)


# -- FILES-CLEANUP-4, -5 walking ---------------------------------------------------------


def test_FILES_CLEANUP_4_symlinks_listed_never_followed(tmp_path: Path) -> None:
    outside = tree(tmp_path / "outside", "keepme.nfo")
    root = tree(tmp_path / "inbox", "a.nfo")
    (root / "to-outside").symlink_to(outside)
    (root / "file-link.nfo").symlink_to(outside / "keepme.nfo")
    p = make_plan(root, delete=["*.nfo", "to-outside"])
    c = classes(p)
    assert c["to-outside"] == "delete"
    assert c["file-link.nfo"] == "delete"
    assert "to-outside/keepme.nfo" not in c
    execute(p, trash=None, run_id="r")
    assert (outside / "keepme.nfo").is_file()  # the link went, never its target
    assert not (root / "to-outside").exists()
    assert not (root / "file-link.nfo").is_symlink()


def test_FILES_CLEANUP_5_hidden_files_are_included(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", ".DS_Store", ".hidden/x.nfo")
    c = classes(make_plan(root, delete=[".DS_Store", "*.nfo"]))
    assert c[".DS_Store"] == "delete"
    assert c[".hidden/x.nfo"] == "delete"
    assert c[".hidden"] == "empty"


# -- FILES-CLEANUP-6, -7, -17 protection ---------------------------------------------------


def test_FILES_CLEANUP_6_library_files_are_never_deleted(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a/01.flac", "a/info.nfo")
    p = make_plan(root, delete=["**"], known={root / "a/01.flac"}, allow_audio=True)
    assert classes(p)["a/01.flac"] == "in_library"
    assert classes(p)["a/info.nfo"] == "delete"


def test_FILES_CLEANUP_7_busy_folders_are_skipped_whole(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "Pending/a.nfo", "Pending/01.flac", "Done/b.nfo")
    busy = [BusyFolder(root / "Pending", "import decision 01DEC")]
    p = make_plan(root, delete=["*.nfo"], busy=busy)
    c = classes(p)
    assert c["Pending/a.nfo"] == "busy"
    assert c["Done/b.nfo"] == "delete"
    assert c["Done"] == "empty"
    assert "Pending" not in c
    assert next(e for e in p.entries if e.path == "Pending/a.nfo").reason == "import decision 01DEC"


def test_FILES_CLEANUP_7_root_inside_a_busy_folder(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox/Album", "a.nfo")
    p = make_plan(root, delete=["*.nfo"], remove_root=True, busy=[BusyFolder(tmp_path, "run R")])
    assert classes(p) == {"a.nfo": "busy"}


def test_FILES_CLEANUP_17_importable_audio_is_left_unless_allowed(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "Unmatched/01.flac", "Unmatched/x.nfo")
    p = make_plan(root, delete=["**/*"])
    assert classes(p)["Unmatched/01.flac"] == "left"
    assert next(e for e in p.entries if e.path == "Unmatched/01.flac").reason == "importable"
    assert "Unmatched" not in classes(p)  # the FLAC stays, so the folder isn't empty
    allowed = make_plan(root, delete=["**/*"], allow_audio=True)
    assert classes(allowed)["Unmatched/01.flac"] == "delete"


# -- FILES-CLEANUP-8 classes ---------------------------------------------------------------


def test_FILES_CLEANUP_8_classify_everything(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo", "rip.log", "cover.jpg", "Scans/", "CD1/x.sfv")
    p = make_plan(root, delete=["*.nfo", "*.sfv", "*.log"], keep=["*.log"])
    assert classes(p) == {
        "a.nfo": "delete",
        "rip.log": "keep",
        "cover.jpg": "left",
        "CD1/x.sfv": "delete",
        "CD1": "empty",
        "Scans": "empty",
    }
    assert p.totals() == {"delete": 2, "keep": 1, "left": 1, "empty": 2}


def test_FILES_CLEANUP_8_nested_empty_folders_deepest_first(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a/b/c/", "a/b/x.nfo")
    p = make_plan(root, delete=["*.nfo"], remove_root=True)
    assert [e.path for e in p.entries if e.cls == "empty"] == ["a/b/c", "a/b", "a", "."]


# -- FILES-CLEANUP-9, -10 cleaning -------------------------------------------------------------


def test_FILES_CLEANUP_9_delete_then_remove_empty_folders(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo", "Scans/", "keep.jpg")
    p = make_plan(root, delete=["*.nfo"])
    out = execute(p, trash=None, run_id="r")
    assert (out.deleted, out.dirs_removed, out.failed) == (1, 1, 0)
    assert out.bytes_freed == len("content of a.nfo")
    assert sorted(os.listdir(root)) == ["keep.jpg"]


def test_FILES_CLEANUP_9_root_removed_only_when_asked(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo")
    execute(make_plan(root, delete=["*.nfo"]), trash=None, run_id="r")
    assert root.is_dir()
    root2 = tree(tmp_path / "inbox2", "a.nfo")
    execute(make_plan(root2, delete=["*.nfo"], remove_root=True), trash=None, run_id="r")
    assert not root2.exists()


def test_FILES_CLEANUP_9_changed_files_are_left(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo")
    p = make_plan(root, delete=["*.nfo"], remove_root=True)
    (root / "a.nfo").write_text("rewritten since the listing, longer")
    out = execute(p, trash=None, run_id="r")
    assert out.changed == 1
    assert (root / "a.nfo").exists()
    assert root.exists()


def test_FILES_CLEANUP_10_a_failed_delete_is_counted(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "locked/a.nfo")
    p = make_plan(root, delete=["*.nfo"])
    (root / "locked").chmod(0o500)
    try:
        out = execute(p, trash=None, run_id="r")
    finally:
        (root / "locked").chmod(0o700)
    assert out.failed == 1
    assert any("can't remove" in line for _, line in out.lines)


def test_FILES_CLEANUP_9_only_reviewed_entries(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo", "b.nfo")
    p = make_plan(root, delete=["*.nfo"])
    out = execute(p, trash=None, run_id="r", only={"a.nfo"})
    assert out.deleted == 1
    assert (root / "b.nfo").exists()


# -- FILES-CLEANUP-15, -16 trash ---------------------------------------------------------------


RUN = "01JZ0000000000000000000000"


def test_FILES_CLEANUP_15_trash_keeps_relative_paths_and_never_overwrites(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    root = tree(tmp_path / "inbox", "CD1/a.nfo")
    execute(make_plan(root, delete=["*.nfo"]), trash=trash, run_id=RUN)
    assert (trash / RUN / "CD1/a.nfo").read_text() == "content of CD1/a.nfo"
    assert (trash / TRASH_MARKER).is_file()
    tree(root, "CD1/a.nfo")
    out = execute(make_plan(root, delete=["*.nfo"]), trash=trash, run_id=RUN)
    assert out.moved == 1
    assert (trash / RUN / "CD1/a.1.nfo").is_file()


def test_FILES_CLEANUP_15_trash_guards(tmp_path: Path) -> None:
    root = tree(tmp_path / "inbox", "a.nfo")
    with pytest.raises(CleanupError, match="inside"):
        check_trash(root / "trash", root)
    foreign = tree(tmp_path / "foreign", "stuff.txt")
    with pytest.raises(CleanupError, match="wasn't made by Rifferr"):
        check_trash(foreign, root)
    assert check_trash(tmp_path / "new-trash", root) == (tmp_path / "new-trash").resolve()
    (foreign / TRASH_MARKER).touch()
    assert check_trash(foreign, root) == foreign.resolve()


def test_FILES_CLEANUP_16_purge_old_run_folders_only(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    old, new = "01JZ0000000000000000000001", "01JZ0000000000000000000002"
    tree(trash, f"{old}/a.nfo", f"{new}/b.nfo", "not-a-run/c.nfo", TRASH_MARKER)
    now = datetime.now(UTC)
    stamp = (now - timedelta(days=40)).timestamp()
    for p in (trash / old / "a.nfo", trash / old):
        os.utime(p, (stamp, stamp))
    removed, others = purge_trash(trash, timedelta(days=30), now)
    assert removed == [old]
    assert others == ["not-a-run"]
    assert (trash / new / "b.nfo").exists()
    assert (trash / "not-a-run/c.nfo").exists()


def test_FILES_CLEANUP_16_unmarked_trash_is_never_purged(tmp_path: Path) -> None:
    trash = tree(tmp_path / "trash", "01JZ0000000000000000000001/a.nfo")
    assert purge_trash(trash, timedelta(seconds=0), datetime.now(UTC) + timedelta(days=1)) == (
        [],
        [],
    )
    assert (trash / "01JZ0000000000000000000001/a.nfo").exists()
