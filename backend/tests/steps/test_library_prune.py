"""S-LIBRARY-PRUNE: the ``library.prune`` step through the engine with a FakeBackend whose
item paths point under ``tmp_path``."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Any

import pytest

from rifferr.core.profile import Redactor
from rifferr.core.prune import find_missing
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow
from rifferr.core.workflow.safety import safety_report
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.prune import InMemoryRemovals
from rifferr.steps.library_prune import LibraryPruneStep, drop_albums, file_exists
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = StepTypeRegistry([*ALL, LibraryPruneStep()])


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    harness = Harness()
    harness.engine.registry = REGISTRY
    harness.backend.music_dirs = [tmp_path / "music"]
    (tmp_path / "music").mkdir()
    return harness


def album(h: Harness, root: Path, name: str, present: int, total: int) -> str:
    folder = root / "music" / name
    folder.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for n in range(total):
        path = folder / f"{n:02d}.flac"
        if n < present:
            path.write_bytes(b"x")
        items.append({"title": f"t{n}", "path": str(path)})
    return h.backend.add_album(
        {"albumartist": "Band", "album": name, "path": str(folder), "items": items}
    ).id


def start(h: Harness, apply: str = "review", **params: Any) -> Run:
    import json

    text = "name: prune\nsteps:\n" + textwrap.indent(
        f"- id: prune\n  uses: library.prune\n  apply: {apply}\n  with: {json.dumps(params)}\n",
        "  ",
    )
    wf, doc = load_workflow(text, "wf.yaml")
    return h.engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=h.backend,
        doc=doc,
        redactor=Redactor([]),
        trusted=True,
    )


def outputs(h: Harness, run: Run) -> dict[str, Any]:
    (finished,) = [e.data for e in h.events(run) if e.type == "run.step.finished"]
    return dict(finished["outputs"])


def logs(h: Harness, run: Run) -> str:
    return "\n".join(str(e.data.get("line")) for e in h.events(run) if e.type == "run.step.log")


def remaining(h: Harness) -> set[str]:
    return {str(a.fields["album"]) for a in h.backend.query(_all())}


# -- LIBRARY-PRUNE-1, -2: finding ----------------------------------------------------------


def test_LIBRARY_PRUNE_1_only_albums_with_every_file_gone(h: Harness, tmp_path: Path) -> None:
    album(h, tmp_path, "Gone", 0, 3)
    album(h, tmp_path, "Half", 1, 3)
    album(h, tmp_path, "Here", 2, 2)
    gone, partial = find_missing(h.backend.query(_all()), file_exists(h.backend.music_dirs))
    assert [a.label for a in gone] == ["Band - Gone"]
    assert [(a.label, a.missing, a.items) for a in partial] == [("Band - Half", 2, 3)]


def test_LIBRARY_PRUNE_2_unknown_paths_count_as_present(tmp_path: Path) -> None:
    unmounted = tmp_path / "drive"
    exists = file_exists([unmounted])
    assert exists(str(unmounted / "a.flac")) is None  # the library folder itself is missing
    assert exists(str(tmp_path / "elsewhere.flac")) is False
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "a.flac").write_bytes(b"x")
    os.chmod(locked, 0)
    try:
        if os.geteuid() != 0:
            assert exists(str(locked / "a.flac")) is None
    finally:
        os.chmod(locked, 0o755)


async def test_LIBRARY_PRUNE_2_too_many_gone_fails_and_removes_nothing(
    h: Harness, tmp_path: Path
) -> None:
    for n in range(3):
        album(h, tmp_path, f"A{n}", 0, 1)
    run = start(h, "auto", max_albums=2)
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert "is the library's drive mounted" in str(run.steps[0].error)
    assert h.backend.removed == []


# -- LIBRARY-PRUNE-3: review ---------------------------------------------------------------


async def test_LIBRARY_PRUNE_3_review_leaves_one_decision_and_waits(
    h: Harness, tmp_path: Path
) -> None:
    gone = album(h, tmp_path, "Gone", 0, 2)
    album(h, tmp_path, "Half", 1, 2)
    run = start(h)
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    (decision,) = h.engine.prunes.items.values()  # type: ignore[attr-defined]
    assert [a.album_id for a in decision.albums] == [gone]
    assert [a.label for a in decision.partial] == ["Band - Half"]
    assert any(e.type == "prune.requested" for e in h.events(run))
    assert h.backend.removed == []


# -- LIBRARY-PRUNE-4, -5, -6: dropping -------------------------------------------------------


async def test_LIBRARY_PRUNE_4_auto_drops_entries_and_never_touches_files(
    h: Harness, tmp_path: Path
) -> None:
    album(h, tmp_path, "Gone", 0, 2)
    album(h, tmp_path, "Here", 1, 1)
    before = sorted(p.name for p in (tmp_path / "music").rglob("*"))
    run = start(h, "auto")
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert h.backend.removed == [(h.backend.removed[0][0], False)]
    assert remaining(h) == {"Here"}
    assert sorted(p.name for p in (tmp_path / "music").rglob("*")) == before
    records = list(h.engine.removals.records.values())  # type: ignore[attr-defined]
    assert [(r["label"], r["state"], r["delete_files"]) for r in records] == [
        ("Band - Gone", "removed", False)
    ]


async def test_LIBRARY_PRUNE_5_outputs_are_counts_and_the_log_names_albums(
    h: Harness, tmp_path: Path
) -> None:
    album(h, tmp_path, "Gone", 0, 1)
    album(h, tmp_path, "Half", 1, 2)
    run = start(h, "auto")
    await h.engine.execute(run)
    assert outputs(h, run) == {"removed": 1, "partial": 1, "skipped": 0}
    text = logs(h, run)
    assert "dropped Band - Gone" in text
    assert "partial: Band - Half" in text


def test_LIBRARY_PRUNE_6_files_back_before_removal_are_skipped(h: Harness, tmp_path: Path) -> None:
    gone = album(h, tmp_path, "Back", 0, 1)
    (tmp_path / "music" / "Back" / "00.flac").write_bytes(b"back")
    journal = InMemoryRemovals()
    out = drop_albums(
        h.backend, journal, "r1", [gone, "missing-id"], file_exists(h.backend.music_dirs)
    )
    assert (out.removed, out.skipped) == ([], [gone, "missing-id"])
    assert journal.records == {}
    assert h.backend.removed == []


# -- LIBRARY-PRUNE-7: safety report ----------------------------------------------------------


def test_LIBRARY_PRUNE_7_safety_report_lists_the_step() -> None:
    wf, _ = load_workflow(
        "name: x\nsteps:\n  - id: prune\n    uses: library.prune\n    apply: auto\n", "wf.yaml"
    )
    kinds = {(f.step_id, f.kind) for f in safety_report(wf).findings}
    assert ("prune", "destructive") in kinds
    assert ("prune", "auto_apply") in kinds
    (finding,) = [f for f in safety_report(wf).findings if f.kind == "destructive"]
    assert "files are not touched" in finding.detail


def _all() -> Selection:
    return Selection()
