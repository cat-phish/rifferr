"""S-BEETS-COMMAND against a real beets library in a temp profile (BACKEND-14,
BEETS-COMMAND-6, -11, -12). No real music or beets config is touched."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from rifferr.backends.beets import BeetsBackend
from rifferr.backends.beets.snapshots import parse_id
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow
from rifferr.engine import Engine, InMemoryEventSink, RunState, StepTypeRegistry
from rifferr.steps.beets_command import BeetsCommandStep
from tests.backends.beets.conftest import BeetsEnv

RUN = "01RUNBEETSCOMMAND000000001"
PATHS = "paths:\n  default: $albumartist/$album/$track $title\n"


def test_BACKEND_14_beets_cli_is_python_m_beets_with_the_query_last(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    argv = backend.cli_command(
        "move", ["-a"], Selection(run_id=RUN, query="comp:1 'genre:Hip Hop'")
    )
    assert argv == [
        sys.executable,
        "-m",
        "beets",
        "move",
        "-a",
        f"rifferr_run::^{RUN}$",
        "comp:1",
        "genre:Hip Hop",
    ]
    assert backend.capabilities.cli
    read_only = BeetsBackend(backend.beetsdir, read_only=True)
    assert not read_only.capabilities.cli


async def test_BEETS_COMMAND_11_beet_move_moves_the_runs_files(
    beets_env: BeetsEnv, tmp_path: Path
) -> None:
    backend = beets_env.backend(PATHS)
    backend.import_folder(beets_env.inbox_album(), "quiet", None, run_id=RUN)
    (album,) = backend.query(Selection(run_id=RUN))
    old_paths = sorted(Path(i.path) for i in album.items)
    assert all(p.is_file() for p in old_paths)
    # Rename the album in the library only; `beet move` then moves the files to match.
    from beets.library import Library

    lib = Library(str(beets_env.beetsdir / "library.db"), str(beets_env.music))
    try:
        stored = lib.albums()[0]
        stored.album = "Renamed"
        stored.store()
    finally:
        lib._close()  # pyright: ignore[reportPrivateUsage]

    environ = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path / "home"), "LANG": "C.UTF-8"}
    sink = InMemoryEventSink()
    registry = StepTypeRegistry([BeetsCommandStep(environ=environ)])
    engine = Engine(registry, sink, new_id=lambda: RUN)  # the albums carry this run's marker
    src = textwrap.dedent(
        """\
        name: t
        steps:
          - {id: move, uses: beets.command, with: {command: move, timeout: 2m}}
        """
    )
    wf, doc = load_workflow(src, "wf.yaml")
    run = engine.create_run(
        wf,
        src.encode(),
        profile_name="main",
        profile_vars={},
        backend=backend,
        doc=doc,
        trusted=True,
        work_dir=tmp_path,
    )
    await engine.execute(run)
    logs = [e.data for e in sink.since(RUN, 0) if e.type == "run.step.log"]
    assert run.state is RunState.succeeded, logs
    (moved,) = backend.query(Selection(run_id=RUN))
    new_paths = sorted(Path(i.path) for i in moved.items)
    assert all("Renamed" in str(p) and p.is_file() for p in new_paths)
    assert not any(p.exists() for p in old_paths)
    info = [str(d["line"]) for d in logs if d["stream"] == "info"]
    # The rename happened before the step, so the move changed only paths.
    assert info == ["Fixture Artist - Renamed: path changed"]
    assert run.steps[0].outputs == {"albums": 1, "changed": 1}


def test_LANES_3_beets_cli_query_selects_only_the_pass_albums(beets_env: BeetsEnv) -> None:
    """Contract: beets regexp terms on ``album_id`` (items) and ``id`` (albums, ``-a``)
    match exactly the given ids, so ``beet`` commands see only this pass's lanes."""
    backend = beets_env.backend()
    for fixture in ("album-flac", "single-mp3"):
        backend.import_folder(beets_env.inbox_album(fixture), "quiet", None, run_id=RUN)
    first, second = backend.query(Selection(run_id=RUN))
    selection = Selection(run_id=RUN, album_ids=(second.id,))
    from beets.library import Library

    lib = Library(str(beets_env.beetsdir / "library.db"), str(beets_env.music))
    try:
        item_query = backend.cli_command("ls", [], selection)[4:]
        number = parse_id(second.id)[1]
        assert {i.album_id for i in lib.items(item_query)} == {number}
        album_query = backend.cli_command("ls", ["-a"], selection)[5:]
        assert [a.id for a in lib.albums(album_query)] == [number]
    finally:
        lib._close()  # pyright: ignore[reportPrivateUsage]
    del first
