"""S-LIBRARY-MOVE in the engine: the ``library.move`` step with a FakeBackend."""

from __future__ import annotations

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.core.selection import Selection
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.executor import RunValidationError
from rifferr.engine.moves import InMemoryMoveConflictStore
from rifferr.steps.library_move import LibraryMoveParams, LibraryMoveStep
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = StepTypeRegistry([*ALL, LibraryMoveStep()])
SRC = "/inbox/Abbey Road"
DEST = "/music/The Beatles/Abbey Road"


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.engine.registry = REGISTRY
    return harness


def start(h: Harness, with_: str, apply: str | None = None, **kw: str) -> Run:
    policy = f"    apply: {apply}\n" if apply else ""
    text = (
        "name: into\nsteps:\n  - id: into-library\n    uses: library.move\n"
        f"{policy}    with: {with_}\n"
    )
    run = h.run(text, profile_vars={"library": "/music", **kw})
    album = h.backend.add_album(
        {
            "albumartist": "The Beatles",
            "album": "Abbey Road",
            RUN_MARKER: run.id,
            "path": SRC,
            "items": [{"title": "Come Together", "track": 1}, {"title": "Something", "track": 2}],
        }
    )
    h.backend.album_files(album.id)
    return run


def _all() -> Selection:
    return Selection()


def paths(h: Harness) -> list[str]:
    [album] = h.backend.query(_all())
    return [i.path for i in album.items]


def test_LIBRARY_MOVE_1_defaults() -> None:
    p = LibraryMoveParams.model_validate({})
    assert (p.mode, p.paths, p.directory, p.stamp) == ("move", None, None, None)
    assert [r.match for r in p.rules()] == [["*"]]


async def test_LIBRARY_MOVE_1_default_policy_is_review(h: Harness) -> None:
    run = start(h, '{directory: "{{ vars.library }}"}')
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    [stored] = h.engine.changesets.for_run(run.id)
    assert stored.state == "proposed"
    assert len(stored.changeset.files) == 2
    assert paths(h)[0].startswith(SRC)  # nothing moved before review


async def test_LIBRARY_MOVE_1_auto_moves_into_interpolated_directory(h: Harness) -> None:
    run = start(h, '{directory: "{{ vars.library }}", stamp: {field: date_added}}', apply="auto")
    await h.engine.execute(run)
    assert run.state is RunState.succeeded, h.logs(run, "error")
    assert paths(h) == [f"{DEST}/1 Come Together.flac", f"{DEST}/2 Something.flac"]
    [album] = h.backend.query(_all())
    assert album.fields["date_added"] == h.clock.now().astimezone().strftime("%Y-%m-%d")


async def test_LIBRARY_MOVE_1_copy_mode(h: Harness) -> None:
    run = start(h, '{mode: copy, directory: "{{ vars.library }}"}', apply="auto")
    await h.engine.execute(run)
    assert f"{SRC}/01 Come Together.flac" in h.backend.fs
    assert paths(h)[0].startswith(DEST)


async def test_LIBRARY_MOVE_1_step_paths_override(h: Harness) -> None:
    run = start(
        h,
        '{directory: /music, paths: [{query: default, format: "$album/$title"}]}',
        apply="auto",
    )
    await h.engine.execute(run)
    assert paths(h)[0] == "/music/Abbey Road/Come Together.flac"


def test_LIBRARY_MOVE_1_paths_need_a_default(h: Harness) -> None:
    with pytest.raises(RunValidationError, match="default"):
        start(h, '{paths: [{query: "comp:1", format: "$album"}]}')


def test_LIBRARY_MOVE_2_paths_refuse_workflow_interpolation(h: Harness) -> None:
    with pytest.raises(RunValidationError, match="isn't allowed"):
        start(h, '{paths: [{query: default, format: "{{ vars.library }}/$album"}]}')


async def test_LIBRARY_MOVE_5_conflict_moves_nothing(h: Harness) -> None:
    run = start(h, "{directory: /music}", apply="auto")
    h.backend.add_files({f"{DEST}/1 Come Together.flac": "already here"})
    await h.engine.execute(run)
    assert any("conflict" in line for line in h.logs(run, "error"))
    assert paths(h)[0].startswith(SRC)
    assert h.engine.changesets.for_run(run.id) == []


async def test_LIBRARY_MOVE_5a_conflict_waits_in_review_queue(h: Harness) -> None:
    run = start(h, "{directory: /music, stamp: {field: date_added}}", apply="auto")
    h.backend.add_files({f"{DEST}/1 Come Together.flac": "already here"})
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    moves = h.engine.moves
    assert isinstance(moves, InMemoryMoveConflictStore)
    [conflict] = moves.items.values()
    [album] = h.backend.query(_all())
    assert (conflict.album_id, conflict.state, conflict.src_dir) == (album.id, "pending", SRC)
    assert "already exists" in conflict.conflicts[0]
    assert conflict.settings.directory == "/music"
    assert conflict.settings.stamp is not None
    [lane] = h.engine.lanes.load(run.id).values()
    assert lane.state == "waiting"
    assert [(hold.kind, hold.ref) for hold in lane.holds] == [("move", conflict.id)]
    assert any(e.type == "move.requested" for e in h.events(run))


async def test_LIBRARY_MOVE_3_in_place_album_logs_and_proposes_nothing(h: Harness) -> None:
    run = start(
        h, "{directory: /inbox, paths: [{query: default, format: '$album/0$track $title'}]}"
    )
    await h.engine.execute(run)
    assert any("already in place" in line for line in h.logs(run))
    assert h.engine.changesets.for_run(run.id) == []


async def test_LIBRARY_MOVE_10_file_failure_fails_the_album(h: Harness) -> None:
    run = start(h, "{directory: /music}", apply="auto")
    h.backend.fail_files[f"{SRC}/02 Something.flac"] = "disk full"
    await h.engine.execute(run)
    assert any("disk full" in line for line in h.logs(run, "error"))
    [stored] = h.engine.changesets.for_run(run.id)
    assert stored.state == "partially_applied"
