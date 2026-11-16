# ruff: noqa: F811, RUF059
"""Audit findings SEC-01..09 (boundary B3: files, symlinks, import, cleanup)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from rifferr.core.workflow import load_workflow
from rifferr.core.workflow.safety import safety_report  # noqa: F401
from rifferr.engine import Engine, InMemoryEventSink, RunState, StepTypeRegistry
from rifferr.steps.beets_import import BeetsImportStep
from rifferr.steps.cleanup_plan import CleanupError, check_root, execute, plan
from rifferr.triggers import fs
from rifferr.worker.import_undo import originals
from tests.api.conftest import Api, api  # noqa: F401  (fixture)
from tests.backends.beets.conftest import (
    FIXTURES,
    BeetsEnv,
    beets_env,  # noqa: F401
    seed,
)
from tests.triggers.test_manager import Env, watch_wf
from tests.triggers.test_manager import env as env
from tests.worker.test_import_undo import (  # noqa: F401  (fixtures and helpers)
    RUN,
    Harness,
    add,
    h,
    inbox_folder,
    run_and_undo,
)

# -- SEC-01 -----------------------------------------------------------------------------


@pytest.mark.parametrize("target", ["other", "."])
async def test_SEC_01_watch_does_not_fire_on_symlinked_top_level_folder(
    env: Env, tmp_path: Path, target: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "a.flac").write_bytes(b"x")
    (env.inbox / "a.flac").write_bytes(b"x") if target == "." else None
    (env.inbox / "link").symlink_to(outside if target == "other" else ".")
    env.write("w", watch_wf())
    await env.manager.start()
    await asyncio.sleep(3.0)
    assert [r.input for r in env.runs] == [], "watch fired on a symlinked top-level entry"


# -- SEC-02 -----------------------------------------------------------------------------

IMPORT_WF = """\
name: imp
steps:
  - id: import
    uses: beets.import
    with: {{ path: '{path}', mode: quiet, options: {{ {opts} }} }}
"""


async def _import(
    env: BeetsEnv, path: Path, extra: str = "", trusted: bool = False, opts: str = "move: true"
):
    registry = StepTypeRegistry([BeetsImportStep()])
    backend = env.backend(extra)
    sink = InMemoryEventSink()
    engine = Engine(registry, sink, cancel_grace=0.5)
    text = IMPORT_WF.format(path=path, opts=opts)
    wf, doc = load_workflow(text, "wf.yaml")
    r = engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=backend,
        doc=doc,
        trusted=trusted,
        work_dir=env.root,
    )
    await engine.execute(r)
    return r, backend


async def test_SEC_02_import_refuses_album_with_symlink_leaving_folder(
    beets_env: BeetsEnv, tmp_path: Path
) -> None:
    album = beets_env.inbox_album("album-flac", name="Real")
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(FIXTURES / "single-mp3", elsewhere)
    victim = elsewhere / "01 Lone.mp3"
    (album / "linked").symlink_to(elsewhere)
    r, _ = await _import(beets_env, album)  # move: true
    assert victim.exists(), "file outside the import folder was moved into the library"
    assert r.state is RunState.failed or r.steps[0].outputs.get("imported") == 1


# -- SEC-03 -----------------------------------------------------------------------------


async def test_SEC_03_undo_keeps_files_that_were_moved_not_copied(
    h: Harness, tmp_path: Path
) -> None:
    from rifferr.appdb import AppTriggers

    library = tmp_path / "library"
    h.backend.music_dirs = [library]
    source = inbox_folder(tmp_path)  # untouched: the album was moved in from elsewhere
    fp = fs.input_fingerprint(source)
    assert fp is not None
    AppTriggers(h.db).remember("main", "wf", str(source), fp, RUN)
    add(h, "A", library / "A")
    await run_and_undo(h)
    assert h.backend.removed[-1][1] is False, "undo deleted files it cannot prove are copies"


def test_SEC_03_fingerprint_sees_symlinks(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(b"x")
    music = tmp_path / "music"
    music.mkdir()
    (music / "a.flac").write_bytes(b"x")
    (folder / "lnk").symlink_to(music)
    fp = fs.input_fingerprint(folder)
    assert fp is not None
    assert originals((str(folder), fp)).intact is False


# -- SEC-04 -----------------------------------------------------------------------------


def test_SEC_04_cleanup_does_not_delete_through_swapped_parent(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "x.nfo"
    f.write_bytes(b"data")
    other = tmp_path / "other"
    other.mkdir()
    g = other / "x.nfo"
    g.write_bytes(b"data")
    os.utime(g, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns))
    p = plan(
        Path(os.path.realpath(root)),
        delete=["*.nfo"],
        keep=[],
        empty_dirs=False,
        remove_root=False,
        busy=[],
        library_files=lambda ps: set(),
        importable=lambda ps: set(),
        allow_audio=True,
    )
    shutil.rmtree(root / "sub")
    (root / "sub").symlink_to(other)
    out = execute(p, trash=None, run_id="R")
    assert g.exists(), "file in a different folder was deleted through a swapped parent"
    assert out.changed >= 1


# -- SEC-05 -----------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["home_parent", "library_sub"])
def test_SEC_05_check_root_refuses_home_ancestor_and_library_subfolder(
    tmp_path: Path, case: str
) -> None:
    home = tmp_path / "users" / "me"
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    home.mkdir(parents=True)
    root = home.parent if case == "home_parent" else lib / "sub"
    with pytest.raises(CleanupError):
        check_root(root, [lib], home)


# -- SEC-06 -----------------------------------------------------------------------------


def test_SEC_06_cleanup_protects_library_files_under_symlinked_parent(
    beets_env: BeetsEnv, tmp_path: Path
) -> None:
    real = tmp_path / "mnt" / "inbox"
    shutil.copytree(FIXTURES / "album-flac", real / "A")
    link = tmp_path / "music-link"
    link.symlink_to(tmp_path / "mnt")
    backend = beets_env.backend()
    stored = link / "inbox" / "A" / "01 First.flac"
    seed(backend, [{"album": "A", "albumartist": "X", "items": [{"title": "t"}]}])
    with backend.library() as lib:
        item = next(iter(lib.items()))
        item.path = os.fsencode(os.path.normpath(os.path.abspath(stored)))
        item.store()
    real_file = real / "A" / "01 First.flac"
    assert real_file in backend.library_files([real_file])


def test_SEC_06_cleanup_protects_album_art(beets_env: BeetsEnv, tmp_path: Path) -> None:
    folder = tmp_path / "inbox" / "A"
    folder.mkdir(parents=True)
    art = folder / "cover.jpg"
    art.write_bytes(b"jpg")
    backend = beets_env.backend()
    seed(backend, [{"album": "A", "albumartist": "X", "items": [{"title": "t"}]}])
    with backend.library() as lib:
        album = next(iter(lib.albums()))
        album.artpath = os.fsencode(art)
        album.store()
    assert art in backend.library_files([art])


# -- SEC-07 -----------------------------------------------------------------------------


async def test_SEC_07_manual_input_outside_watch_folder_is_refused(
    api: Api, tmp_path: Path
) -> None:
    inbox = tmp_path / "inbox"
    other = tmp_path / "other"
    inbox.mkdir()
    other.mkdir()
    api.add_workflow(
        "w",
        f"name: w\non:\n  watch: {{path: '{inbox}'}}\n"
        "steps:\n  - {id: a, uses: test.echo, with: {message: '{{ run.input }}'}}\n",
    )
    r = await api.client.post(
        "/api/runs", json={"profile": "main", "workflow": "w", "vars": {}, "input": str(other)}
    )
    assert 400 <= r.status_code < 500, f"run with input outside the watch folder: {r.status_code}"


# -- SEC-08 -----------------------------------------------------------------------------


async def test_SEC_08_untrusted_import_cannot_remove_duplicates(
    beets_env: BeetsEnv,
) -> None:
    beets_env.inbox_album("album-flac", name="first")
    r1, backend = await _import(beets_env, beets_env.inbox / "first", trusted=False, opts="")
    assert r1.state is RunState.succeeded
    old = sorted(beets_env.music.rglob("*.flac"))
    assert old
    beets_env.inbox_album("album-flac", name="second")
    r2, _ = await _import(
        beets_env,
        beets_env.inbox / "second",
        trusted=False,
        opts="autotag: false, duplicate_action: remove",
    )
    assert r2.state is RunState.failed, "untrusted workflow ran duplicate_action: remove"
    assert all(f.exists() for f in old), "existing library files were deleted"


# -- SEC-09 -----------------------------------------------------------------------------


def test_SEC_09_save_script_does_not_write_through_symlink(tmp_path: Path) -> None:
    from rifferr.script_store import save_script, scripts_dir

    profile = tmp_path / "profile"
    scripts_dir(profile).mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    (scripts_dir(profile) / ".s.yaml.tmp").symlink_to(victim)
    save_script(profile, "s", "actions: []\n", None)
    assert victim.read_text() == "keep", "temp file name was written through a pre-existing symlink"
