"""S-FILES-CLEANUP in the engine: the ``files.cleanup`` step.

Walking and classifying are covered in ``tests/files/test_cleanup_plan.py``; these tests
run the step through the engine with a FakeBackend and a folder under ``tmp_path``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.base import RUN_MARKER
from rifferr.core.files import BusyFolder
from rifferr.core.profile import FilesSettings, ProfileFile, Redactor
from rifferr.core.workflow import load_workflow
from rifferr.core.workflow.safety import safety_report
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.executor import RunValidationError
from rifferr.steps.cleanup_plan import TRASH_MARKER
from rifferr.steps.files_cleanup import FilesCleanupStep, apply_reviewed
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

REGISTRY = StepTypeRegistry([*ALL, FilesCleanupStep()])


@pytest.fixture
def h() -> Harness:
    harness = Harness()
    harness.engine.registry = REGISTRY
    return harness


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    root = tmp_path / "inbox"
    (root / "Album" / "Scans").mkdir(parents=True)
    (root / "Album" / "info.nfo").write_text("nfo")
    (root / "Album" / "Scans" / "front.jpg").write_bytes(b"jpg")
    (root / "Album" / "notes.txt").write_text("keep me")
    return root


def start(
    h: Harness,
    steps: str,
    *,
    trusted: bool = True,
    settings: ProfileFile | None = None,
    busy: list[BusyFolder] | None = None,
) -> Run:
    text = "name: sweep\nsteps:\n" + textwrap.indent(textwrap.dedent(steps), "  ")
    wf, doc = load_workflow(text, "wf.yaml")
    return h.engine.create_run(
        wf,
        text.encode(),
        profile_name="main",
        profile_vars={},
        backend=h.backend,
        doc=doc,
        redactor=Redactor([]),
        trusted=trusted,
        profile_settings=settings,
        busy=(lambda: list(busy)) if busy is not None else None,
    )


def step(path: Path, **params: Any) -> str:
    import json

    return "- id: clean\n  uses: files.cleanup\n  with: " + json.dumps(
        {"path": str(path), **params}
    )


def events(h: Harness, run: Run, type: str) -> list[dict[str, Any]]:
    return [dict(e.data) for e in h.events(run) if e.type == type]


def outputs(h: Harness, run: Run) -> dict[str, Any]:
    (finished,) = events(h, run, "run.step.finished")
    return dict(finished["outputs"])


# -- FILES-CLEANUP-1, -2: parameters -----------------------------------------------------


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"mode": "delete", "empty_dirs": False}, "nothing to do"),
        ({"review": True}, "review only applies to mode delete"),
        ({"mode": "delete", "delete": ["*.nfo"], "on_timeout": "delete"}, "needs a timeout"),
        ({"mode": "delete", "delete": ["*.nfo"], "timeout": "1h"}, "only applies with review"),
        ({"mode": "wipe"}, "mode"),
    ],
)
def test_FILES_CLEANUP_1_bad_parameters_fail_validation(
    h: Harness, inbox: Path, params: dict[str, Any], message: str
) -> None:
    with pytest.raises(RunValidationError, match=message):
        start(h, step(inbox, **params))


def test_FILES_CLEANUP_1_defaults_report_mode(h: Harness, inbox: Path) -> None:
    params = FilesCleanupStep.params_model.model_validate({"path": str(inbox)})
    assert (params.mode, params.empty_dirs, params.remove_root, params.allow_audio) == (
        "report",
        True,
        False,
        False,
    )


def test_FILES_CLEANUP_2_when_is_a_validation_error(h: Harness, inbox: Path) -> None:
    with pytest.raises(RunValidationError, match="when"):
        start(h, step(inbox) + "\n  when: 'album:x'")


async def test_FILES_CLEANUP_2_runs_once_per_run_not_per_album(h: Harness, inbox: Path) -> None:
    run = start(h, step(inbox))
    for name in ("A", "B"):
        h.backend.add_album({"album": name, RUN_MARKER: run.id, "items": [{"title": "t"}]})
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert len(events(h, run, "run.step.cleanup.report")) == 1


# -- FILES-CLEANUP-3: the folder must be safe ------------------------------------------


async def test_FILES_CLEANUP_3_library_folder_fails_the_step(h: Harness, inbox: Path) -> None:
    h.backend.music_dirs = [inbox / "Album"]  # the folder holds the library
    run = start(h, step(inbox))
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert inbox.joinpath("Album", "info.nfo").exists()


# -- FILES-CLEANUP-8, -11: report --------------------------------------------------------


async def test_FILES_CLEANUP_8_report_mode_lists_and_changes_nothing(
    h: Harness, inbox: Path
) -> None:
    run = start(h, step(inbox, delete=["*.nfo", "Album/Scans/*.jpg"]), trusted=False)
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    (report,) = events(h, run, "run.step.cleanup.report")
    classes = {e["path"]: e["cls"] for e in report["entries"]}
    assert classes["Album/info.nfo"] == "delete"
    assert classes["Album/Scans/front.jpg"] == "delete"
    assert classes["Album/notes.txt"] == "left"
    assert report["truncated"] == 0
    assert all("mtime_ns" not in e for e in report["entries"])
    assert inbox.joinpath("Album", "info.nfo").exists()
    assert outputs(h, run)["to_delete"] == 2


async def test_FILES_CLEANUP_11_left_files_are_logged_as_a_warning(h: Harness, inbox: Path) -> None:
    run = start(h, step(inbox, delete=["*.nfo"]))
    await h.engine.execute(run)
    errors = h.logs(run, "error")
    assert any("Album/notes.txt" in line for line in errors)
    assert any("check them by hand" in line for line in errors)
    assert outputs(h, run)["left"] == 2


# -- FILES-CLEANUP-9, -10: delete --------------------------------------------------------


async def test_FILES_CLEANUP_9_delete_mode_removes_matches_and_empty_folders(
    h: Harness, inbox: Path
) -> None:
    run = start(h, step(inbox, mode="delete", delete=["*.nfo", "*.jpg"]))
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert not inbox.joinpath("Album", "info.nfo").exists()
    assert not inbox.joinpath("Album", "Scans").exists()
    assert inbox.joinpath("Album", "notes.txt").exists()
    assert inbox.exists()  # remove_root is false
    out = outputs(h, run)
    assert (out["deleted"], out["dirs_removed"], out["failed"], out["bytes_freed"]) == (2, 1, 0, 6)


async def test_FILES_CLEANUP_10_a_failed_deletion_fails_the_step(
    h: Harness, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "info.nfo":
            raise PermissionError(13, "Permission denied")
        real(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    run = start(h, step(inbox, mode="delete", delete=["*.nfo", "*.jpg"]))
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert not inbox.joinpath("Album", "Scans", "front.jpg").exists()  # it went on
    assert any("can't remove" in line for line in h.logs(run, "error"))


# -- FILES-CLEANUP-7: busy folders from the server -------------------------------------


async def test_FILES_CLEANUP_7_busy_folders_are_skipped(h: Harness, inbox: Path) -> None:
    busy = [BusyFolder(inbox / "Album", "pending import decision")]
    run = start(h, step(inbox, mode="delete", delete=["*.nfo"]), busy=busy)
    await h.engine.execute(run)
    assert inbox.joinpath("Album", "info.nfo").exists()
    assert outputs(h, run)["busy"] >= 1


# -- FILES-CLEANUP-13: trust -----------------------------------------------------------


async def test_FILES_CLEANUP_13_delete_mode_is_refused_untrusted(h: Harness, inbox: Path) -> None:
    run = start(h, step(inbox, mode="delete", delete=["*.nfo"]), trusted=False)
    await h.engine.execute(run)
    assert run.state is RunState.failed
    assert inbox.joinpath("Album", "info.nfo").exists()
    assert any("trusted" in str(e.get("error")) for e in events(h, run, "run.step.finished"))


def test_FILES_CLEANUP_13_safety_report_lists_delete_mode_as_destructive(inbox: Path) -> None:
    text = (
        "name: sweep\nsteps:\n"
        + textwrap.indent(step(inbox, mode="delete", delete=["*.nfo"], trash="/t"), "  ")
        + "\n"
        + textwrap.indent(step(inbox).replace("id: clean", "id: look"), "  ")
    )
    wf, _ = load_workflow(text, "wf.yaml")
    (finding,) = safety_report(wf).of_kind("destructive")
    assert finding.step_id == "clean"
    assert str(inbox) in finding.detail
    assert "*.nfo" in finding.detail
    assert "/t" in finding.detail


def test_FILES_CLEANUP_17_safety_report_mentions_allow_audio(inbox: Path) -> None:
    text = "name: sweep\nsteps:\n" + textwrap.indent(
        step(inbox, mode="delete", delete=["*"], allow_audio=True), "  "
    )
    wf, _ = load_workflow(text, "wf.yaml")
    (finding,) = safety_report(wf).of_kind("destructive")
    assert "allow_audio" in finding.detail


async def test_FILES_CLEANUP_17_importable_audio_survives_delete(h: Harness, inbox: Path) -> None:
    inbox.joinpath("Album", "01.flac").write_bytes(b"flac")
    run = start(h, step(inbox, mode="delete", delete=["*"]))
    await h.engine.execute(run)
    assert inbox.joinpath("Album", "01.flac").exists()
    assert not inbox.joinpath("Album", "notes.txt").exists()


# -- FILES-CLEANUP-15, -16: trash ------------------------------------------------------


async def test_FILES_CLEANUP_15_trash_keeps_the_layout_under_the_run_id(
    h: Harness, inbox: Path, tmp_path: Path
) -> None:
    trash = tmp_path / "trash"
    run = start(h, step(inbox, mode="delete", delete=["*.jpg"], trash=str(trash)))
    await h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert trash.joinpath(run.id, "Album", "Scans", "front.jpg").read_bytes() == b"jpg"
    assert trash.joinpath(TRASH_MARKER).exists()
    assert outputs(h, run)["moved"] == 1


async def test_FILES_CLEANUP_16_old_trash_is_emptied_at_step_start(
    h: Harness, inbox: Path, tmp_path: Path
) -> None:
    import os

    trash = tmp_path / "trash"
    old = trash / "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    old.mkdir(parents=True)
    (trash / TRASH_MARKER).touch()
    (old / "x.jpg").write_bytes(b"x")
    os.utime(old / "x.jpg", (0, 0))
    os.utime(old, (0, 0))
    settings = ProfileFile(files=FilesSettings(trash_keep="7d"))
    run = start(h, step(inbox, trash=str(trash)), settings=settings)
    await h.engine.execute(run)
    assert not old.exists()
    assert any("emptied run folder" in line for line in h.logs(run))


# -- FILES-CLEANUP-14: review ----------------------------------------------------------


async def test_FILES_CLEANUP_14_review_deletes_nothing_and_holds_the_run(
    h: Harness, inbox: Path
) -> None:
    run = start(h, step(inbox, mode="delete", delete=["*.nfo"], review=True, timeout="1h"))
    await h.engine.execute(run)
    assert run.state is RunState.waiting_review
    assert inbox.joinpath("Album", "info.nfo").exists()
    (requested,) = events(h, run, "cleanup.requested")
    decision = h.engine.cleanups.get(str(requested["decision_id"]))
    assert decision is not None
    assert (decision.state, decision.on_timeout, decision.path) == ("pending", "keep", str(inbox))
    assert decision.deadline is not None
    (lane,) = h.engine.lanes.load(run.id).values()
    assert [(x.kind, x.ref) for x in lane.holds] == [("cleanup", decision.id)]
    assert outputs(h, run)["decision"] == decision.id


async def test_FILES_CLEANUP_14_apply_removes_only_reviewed_unchanged_entries(
    h: Harness, inbox: Path
) -> None:
    run = start(h, step(inbox, mode="delete", delete=["*.nfo", "*.jpg"], review=True))
    await h.engine.execute(run)
    (requested,) = events(h, run, "cleanup.requested")
    decision = h.engine.cleanups.get(str(requested["decision_id"]))
    assert decision is not None
    inbox.joinpath("Album", "Scans", "front.jpg").write_bytes(b"changed!")  # changed
    inbox.joinpath("Album", "new.nfo").write_text("new")  # not reviewed
    outcome = apply_reviewed(decision, h.backend, [], run.id)
    assert not inbox.joinpath("Album", "info.nfo").exists()
    assert inbox.joinpath("Album", "Scans", "front.jpg").exists()
    assert inbox.joinpath("Album", "new.nfo").exists()
    assert (outcome.deleted, outcome.changed) == (1, 1)
