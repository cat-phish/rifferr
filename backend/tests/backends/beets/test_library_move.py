"""S-LIBRARY-MOVE with beets: destinations from beets path formats (contract tests) and
journaled moves of real files."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.beets import BeetsBackend
from rifferr.backends.localfs import TMP_SUFFIX
from rifferr.core.changeset import ChangeSet, ChangeSource, JournalEntry
from rifferr.core.moves import ArtifactRule, MovePlan, PathFormat
from rifferr.core.selection import Selection
from rifferr.core.workflow import load_workflow
from rifferr.engine import InMemoryJournal
from rifferr.engine.changesets import ApplyOutcome, InMemoryChangeSetStore, apply_changeset
from rifferr.engine.recovery import recover_pending
from rifferr.engine.undo import UndoContext, undo_entry
from tests.backends.beets.conftest import BeetsEnv

RUN = "01RUNLIBRARYMOVE000000001"
REGULAR = (
    "%the{$albumartist}/%if{$original_year,($original_year) }$album "
    "[%if{%ifgreater{$bitdepth,16,1},24-bit }$format]/"
    "%ifgreater{$disctotal,1,Disc %unpad{$disc}/}$track. $title"
)
SPLIT = (
    "%the{$albumartist}/$album%if{$original_year, ($original_year)} "
    "[%if{%ifgreater{$bitdepth,16,1},24-bit }$format]/"
    "%ifgreater{$disctotal,1,Disc %unpad{$disc}/}$track. $title"
)
CONFIG = f"""\
plugins: [the]
the:
  a: no
  format: "{{0}}, {{1}}"
paths:
  "albumkind::^(split|compilation)$": "{SPLIT}"
  default: "{REGULAR}"
"""


class Env:
    def __init__(self, beets_env: BeetsEnv, **fields: Any) -> None:
        self.env = beets_env
        self.backend: BeetsBackend = beets_env.backend(CONFIG)
        self.folder = beets_env.inbox_album()
        self.backend.import_folder(
            self.folder, "quiet", None, run_id=RUN, options={"copy": False, "move": False}
        )
        self.library = beets_env.root / "library2"
        self.set(
            albumartist="The Beatles",
            album="Abbey Road",
            original_year=1969,
            **fields,
        )
        self.journal = InMemoryJournal()
        self.store = InMemoryChangeSetStore()
        self._ids = count(1)

    def set(self, **fields: Any) -> None:
        with self.backend.library() as lib:
            [album] = lib.albums()
            for key, value in fields.items():
                album[key] = value
            album.store()
            for n, item in enumerate(album.items(), start=1):
                item.title = ["Come Together", "Something"][n - 1]
                item.track = n
                item.store()

    def item(self, **fields: Any) -> None:
        with self.backend.library() as lib:
            for item in lib.items():
                for key, value in fields.items():
                    item[key] = value
                item.store()

    def plan(self, **kw: Any) -> MovePlan:
        kw.setdefault("mode", "move")
        kw.setdefault("formats", None)
        kw.setdefault("directory", self.library)
        kw.setdefault("artifacts", [ArtifactRule(match=["*"])])
        [plan] = self.backend.plan_moves(Selection(run_id=RUN), **kw)
        return plan

    def rel(self, plan: MovePlan) -> list[str]:
        root = os.path.realpath(self.library)
        return [os.path.relpath(f.dst, root) for f in plan.files]

    def new_id(self) -> str:
        return f"id-{next(self._ids):03d}"

    def now(self) -> datetime:
        return datetime.now(UTC)

    def emitter_for(self, run_id: str | None, step_id: str | None) -> Any:
        return lambda type, data: None

    def apply(self, plan: MovePlan) -> ApplyOutcome:
        assert not plan.conflicts, plan.conflicts
        cs = ChangeSet(
            id=self.new_id(),
            profile="test",
            run_id=RUN,
            source=ChangeSource(kind="step", name="library.move"),
            created_at=self.now(),
            targets=[],
            files=plan.files,
        )
        self.store.add(cs)
        return apply_changeset(
            cs,
            backend=self.backend,
            journal=self.journal,
            store=self.store,
            new_id=self.new_id,
            now=self.now,
            emit=lambda type, data: None,
        )

    def paths(self) -> list[str]:
        [album] = self.backend.query(Selection(run_id=RUN))
        return [i.path for i in album.items]


@pytest.fixture
def env(beets_env: BeetsEnv) -> Env:
    return Env(beets_env)


def test_LIBRARY_MOVE_3_beets_destination_from_reference_pattern(env: Env) -> None:
    assert env.rel(env.plan()) == [
        "Beatles, The/(1969) Abbey Road [FLAC]/01. Come Together.flac",
        "Beatles, The/(1969) Abbey Road [FLAC]/02. Something.flac",
    ]


def test_LIBRARY_MOVE_3_disc_folder_only_for_multi_disc(env: Env) -> None:
    env.set(disctotal=2)  # beets reads album-level fields from the album in path formats
    env.item(disctotal=2, disc=1)
    assert env.rel(env.plan())[0] == (
        # beets pads $disc in paths; %unpad matches foobar2000's %discnumber% (question 2)
        "Beatles, The/(1969) Abbey Road [FLAC]/Disc 1/01. Come Together.flac"
    )


def test_LIBRARY_MOVE_3_24_bit_marked_in_folder_name(env: Env) -> None:
    env.item(bitdepth=24)
    assert env.rel(env.plan())[0].startswith("Beatles, The/(1969) Abbey Road [24-bit FLAC]/")


def test_LIBRARY_MOVE_3_query_specific_format_for_splits(env: Env) -> None:
    env.set(albumkind="split")
    assert env.rel(env.plan())[0] == "Beatles, The/Abbey Road (1969) [FLAC]/01. Come Together.flac"


def test_LIBRARY_MOVE_2_step_paths_override_profile_paths(env: Env) -> None:
    plan = env.plan(formats=[PathFormat(query="default", format="$album/$title")])
    assert env.rel(plan)[0] == "Abbey Road/Come Together.flac"


EXAMPLE = Path(__file__).parents[4] / "examples" / "workflows" / "foobar-style.yaml"


def test_LIBRARY_MOVE_3_reference_workflow_paths_match_the_pattern(env: Env) -> None:
    workflow, _ = load_workflow(EXAMPLE.read_text(), str(EXAMPLE))
    moves = {s.id: s for s in workflow.steps if s.uses == "library.move"}
    assert set(moves) == {"to-staging", "into-library"}
    formats = [PathFormat.model_validate(p) for p in moves["into-library"].with_["paths"]]
    assert [f.format for f in formats] == [SPLIT, REGULAR]
    # Staging uses the same names, so the final move only changes the root folder.
    assert moves["to-staging"].with_["paths"] == moves["into-library"].with_["paths"]
    env.set(disctotal=2)
    env.item(disctotal=2, disc=1)
    assert env.rel(env.plan(formats=formats))[0] == (
        "Beatles, The/(1969) Abbey Road [FLAC]/Disc 1/01. Come Together.flac"
    )


def test_LIBRARY_MOVE_3_beets_replace_rules_apply(env: Env) -> None:
    env.set(album="AC/DC Live")
    assert "AC_DC Live" in env.rel(env.plan())[0]


def test_LIBRARY_MOVE_5_symlinked_destination_out_of_directory_conflicts(env: Env) -> None:
    outside = env.env.root / "elsewhere"
    outside.mkdir()
    env.library.mkdir()
    (env.library / "Beatles, The").symlink_to(outside)
    assert any("outside" in c for c in env.plan().conflicts)


def test_LIBRARY_MOVE_8_moves_real_files_and_updates_beets(env: Env) -> None:
    (env.folder / "rip.log").write_text("log")
    outcome = env.apply(env.plan())
    assert outcome.state == "applied", outcome.error
    root = os.path.realpath(env.library)
    assert all(p.startswith(root) for p in env.paths())
    assert all(Path(p).is_file() for p in env.paths())
    assert (Path(root) / "Beatles, The/(1969) Abbey Road [FLAC]/rip.log").read_text() == "log"
    assert not list(env.folder.glob("*.flac"))
    assert not list(Path(root).rglob(f"*{TMP_SUFFIX}"))


def test_LIBRARY_MOVE_8_copy_leaves_originals(env: Env) -> None:
    outcome = env.apply(env.plan(mode="copy"))
    assert outcome.state == "applied"
    assert len(list(env.folder.glob("*.flac"))) == 2
    assert all(p.startswith(os.path.realpath(env.library)) for p in env.paths())


def test_LIBRARY_MOVE_11_empty_source_folder_is_left(env: Env) -> None:
    env.apply(env.plan())
    assert env.folder.is_dir()
    assert list(env.folder.iterdir()) == []


def test_LIBRARY_MOVE_12_recovery_removes_temporary_files(env: Env) -> None:
    plan = env.plan(mode="copy")
    cs = ChangeSet(
        id="cs-crash",
        profile="test",
        run_id=RUN,
        source=ChangeSource(kind="step", name="library.move"),
        created_at=env.now(),
        targets=[],
        files=plan.files,
    )
    env.store.add(cs)
    entry = JournalEntry(
        id="j-crash",
        profile="test",
        changeset_id=cs.id,
        run_id=RUN,
        intended=[],
        files=plan.files,
        created_at=env.now(),
    )
    env.journal.begin(entry)
    env.backend.copy_file(plan.files[0])
    first = Path(plan.files[1].dst)
    first.parent.mkdir(parents=True, exist_ok=True)
    tmp = first.parent / f".{first.name}{TMP_SUFFIX}"
    tmp.write_bytes(b"half")
    [done] = recover_pending(
        "test",
        journal=env.journal,
        backend=env.backend,
        store=env.store,
        emitter_for=env.emitter_for,
        now=env.now,
    )
    assert [f.state for f in done.files] == ["copied", "planned"]
    assert not tmp.exists()
    assert env.paths()[0] == plan.files[0].dst


def test_LIBRARY_MOVE_13_undo_puts_files_and_paths_back(env: Env) -> None:
    before = env.paths()
    outcome = env.apply(env.plan())
    assert outcome.entry is not None
    ctx = UndoContext(
        backend=env.backend,
        journal=env.journal,
        store=env.store,
        new_id=env.new_id,
        now=env.now,
        emitter_for=env.emitter_for,
    )
    assert undo_entry(outcome.entry.id, ctx).undone
    assert env.paths() == before
    assert all(Path(p).is_file() for p in before)
