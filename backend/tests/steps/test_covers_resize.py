"""S-COVERS-RESIZE in the engine: the ``covers.resize`` step with a FakeBackend."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from PIL import Image
from pydantic import ValidationError

from rifferr.artstore import ArtStore
from rifferr.backends.base import RUN_MARKER, BackendCapabilities
from rifferr.core.covers import ART_FIELD, sniff_image
from rifferr.core.selection import Selection
from rifferr.covers.resize import PillowResizer, load_resizers
from rifferr.engine import Run, RunState, StepTypeRegistry
from rifferr.engine.undo import UndoContext, undo_entry
from rifferr.steps.covers_resize import CoversResizeParams, CoversResizeStep
from tests.engine.conftest import Harness
from tests.engine.steps import ALL

FOLDER = "/music/Artist/Album"
ART = f"{FOLDER}/cover.jpg"
KEPT = f"{FOLDER}/cover-hires.jpg"


def image(width: int, height: int, fmt: str = "JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format=fmt)
    return buf.getvalue()


class H:
    def __init__(self, tmp: Path) -> None:
        self.h = Harness()
        self.store = ArtStore(tmp / "art")
        self.h.engine.registry = StepTypeRegistry(
            [*ALL, CoversResizeStep({"pillow": PillowResizer()}, art_root=self.store.root)]
        )

    @property
    def backend(self) -> Any:
        return self.h.backend

    def start(self, cover: bytes | None, with_: str = "{max: 1200}", **step: str) -> Run:
        extra = "".join(f"    {k}: {v}\n" for k, v in step.items())
        text = (
            "name: art\nsteps:\n  - id: art-size\n    uses: covers.resize\n"
            f"{extra}    with: {with_}\n"
        )
        run = self.h.run(text)
        fields: dict[str, Any] = {"albumartist": "Artist", "album": "Album", RUN_MARKER: run.id}
        if cover is not None:
            sha, stored = self.store.put(cover)
            fields |= {ART_FIELD: sha, "artpath": ART}
            # The fake's filesystem: the cover and the art store's copy of it.
            self.backend.add_files({ART: cover, str(stored): cover})
        self.backend.add_album({**fields, "path": FOLDER, "items": [{"title": "t", "track": 1}]})
        return run

    def album(self) -> Any:
        [album] = self.backend.query(Selection())
        return album

    def art_size(self) -> tuple[int, int]:
        found = self.store.find(self.album().fields[ART_FIELD])
        assert found is not None
        info = sniff_image(found.read_bytes())
        assert info is not None
        return info.width, info.height

    def undo(self, entry_id: str) -> Any:
        events: list[Any] = []
        return undo_entry(
            entry_id,
            UndoContext(
                backend=self.backend,
                journal=self.h.engine.journal,
                store=self.h.engine.changesets,
                new_id=lambda: "undo-1",
                now=self.h.clock.now,
                emitter_for=lambda run, step: lambda type, data: events.append((type, data)),
                by="tester",
            ),
        )


@pytest.fixture
def h(tmp_path: Path) -> H:
    return H(tmp_path)


# -- COVERS-RESIZE-1: keep the original, then shrink the cover --------------------------


async def test_COVERS_RESIZE_1_large_cover_kept_and_shrunk(h: H) -> None:
    big = image(3000, 2000)
    run = h.start(big)
    await h.h.engine.execute(run)
    assert run.state is RunState.succeeded
    [stored] = h.h.engine.changesets.for_run(run.id)
    cs = stored.changeset
    assert stored.state == "applied"
    [effect] = cs.files
    assert (effect.kind, effect.mode, effect.dst) == ("artifact", "copy", KEPT)
    assert [c.field for t in cs.targets for c in t.changes] == [ART_FIELD]
    assert cs.rationale == "cover 3000x2000 -> 1200x800; original kept as cover-hires.jpg"
    assert h.backend.fs[KEPT] == big  # the original, byte for byte
    assert h.art_size() == (1200, 800)  # longest side = max, aspect kept


# -- COVERS-RESIZE-2: nothing to do ----------------------------------------------------


@pytest.mark.parametrize("cover", [None, image(1200, 1200), image(800, 1100)])
async def test_COVERS_RESIZE_2_no_cover_or_small_enough_is_skipped(
    h: H, cover: bytes | None
) -> None:
    run = h.start(cover)
    await h.h.engine.execute(run)
    assert run.state is RunState.succeeded
    assert h.h.engine.changesets.for_run(run.id) == []
    assert KEPT not in h.backend.fs


# -- COVERS-RESIZE-3: idempotent --------------------------------------------------------


@settings(
    max_examples=8, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    width=st.integers(min_value=1, max_value=2400),
    height=st.integers(min_value=1, max_value=2400),
    max_side=st.integers(min_value=50, max_value=1600),
)
async def test_COVERS_RESIZE_3_second_run_changes_nothing(
    tmp_path_factory: pytest.TempPathFactory, width: int, height: int, max_side: int
) -> None:
    h = H(tmp_path_factory.mktemp("resize"))
    run = h.start(image(width, height), f"{{max: {max_side}}}")
    await h.h.engine.execute(run)
    assert max(h.art_size()) == min(max(width, height), max_side)
    after = (h.album().fields[ART_FIELD], dict(h.backend.fs))
    again = h.h.run(
        f"name: art\nsteps:\n  - id: art-size\n    uses: covers.resize\n"
        f"    with: {{max: {max_side}}}\n"
    )
    h.backend._albums[0].fields[RUN_MARKER] = again.id  # the next run's album
    await h.h.engine.execute(again)
    assert h.h.engine.changesets.for_run(again.id) == []
    assert (h.album().fields[ART_FIELD], dict(h.backend.fs)) == after


# -- COVERS-RESIZE-4: never overwrite ---------------------------------------------------


async def test_COVERS_RESIZE_4_other_kept_file_fails_the_album(h: H) -> None:
    h.backend.add_files({KEPT: b"someone else's file"})
    run = h.start(image(2000, 2000))
    await h.h.engine.execute(run)
    assert h.h.engine.changesets.for_run(run.id) == []
    assert h.backend.fs[KEPT] == b"someone else's file"
    lines = h.h.logs(run, "error") + h.h.logs(run)
    assert any("not overwritten" in line for line in lines)


async def test_COVERS_RESIZE_4_identical_kept_file_only_changes_the_cover(h: H) -> None:
    big = image(2000, 2000)
    h.backend.add_files({KEPT: big})
    run = h.start(big)
    await h.h.engine.execute(run)
    [stored] = h.h.engine.changesets.for_run(run.id)
    assert stored.changeset.files == []
    assert stored.state == "applied"
    assert h.art_size() == (1200, 1200)


# -- COVERS-RESIZE-5: undo -----------------------------------------------------------------


async def test_COVERS_RESIZE_5_undo_restores_the_cover_and_removes_the_copy(h: H) -> None:
    big = image(2400, 2400)
    run = h.start(big)
    original_sha = h.album().fields[ART_FIELD]
    await h.h.engine.execute(run)
    [entry] = h.h.engine.journal.for_run(run.id)
    outcome = h.undo(entry.id)
    assert outcome.undone
    assert h.album().fields[ART_FIELD] == original_sha
    assert KEPT not in h.backend.fs


async def test_COVERS_RESIZE_5_undo_refuses_when_the_copy_changed(h: H) -> None:
    run = h.start(image(2400, 2400))
    await h.h.engine.execute(run)
    [entry] = h.h.engine.journal.for_run(run.id)
    h.backend.add_files({KEPT: b"edited since"})
    outcome = h.undo(entry.id)
    assert not outcome.undone
    assert outcome.changed_files == [KEPT]
    assert h.backend.fs[KEPT] == b"edited since"


# -- COVERS-RESIZE-6: apply policy ----------------------------------------------------------


async def test_COVERS_RESIZE_6_review_holds_the_change(h: H) -> None:
    run = h.start(image(2000, 1000), apply="review")
    await h.h.engine.execute(run)
    assert run.state is RunState.waiting_review
    [stored] = h.h.engine.changesets.for_run(run.id)
    assert stored.state == "proposed"
    assert KEPT not in h.backend.fs


def test_COVERS_RESIZE_6_defaults() -> None:
    p = CoversResizeParams.model_validate({"max": 1200})
    assert (p.original, p.format, p.quality, p.resizer) == (
        "{stem}-hires{ext}",
        "keep",
        90,
        "pillow",
    )
    assert CoversResizeStep.default_apply == "auto"
    assert p.kept_name("cover.jpg") == "cover-hires.jpg"
    assert p.kept_name("folder.png") == "folder-hires.png"


@pytest.mark.parametrize("original", ["../x{ext}", "sub/{stem}", "plain.jpg", ""])
def test_COVERS_RESIZE_6_original_must_be_a_new_file_name(original: str) -> None:
    with pytest.raises(ValidationError):
        CoversResizeParams.model_validate({"max": 1200, "original": original})


def test_COVERS_RESIZE_6_max_is_required() -> None:
    with pytest.raises(ValidationError):
        CoversResizeParams.model_validate({})


# -- COVERS-RESIZE-7: output format -----------------------------------------------------------


@pytest.mark.parametrize(
    ("fmt", "source", "mime"),
    [
        ("keep", "PNG", "image/png"),
        ("keep", "JPEG", "image/jpeg"),
        ("jpeg", "PNG", "image/jpeg"),
    ],
)
def test_COVERS_RESIZE_7_output_format(fmt: Any, source: str, mime: str) -> None:
    out = PillowResizer().resize(image(1600, 900, source), 1200, fmt, 90)
    info = sniff_image(out)
    assert info is not None
    assert (info.mime, info.width, info.height) == (mime, 1200, 675)


def test_COVERS_RESIZE_7_rgba_png_to_jpeg() -> None:
    buf = io.BytesIO()
    Image.new("RGBA", (1500, 1500), (0, 0, 0, 0)).save(buf, format="PNG")
    info = sniff_image(PillowResizer().resize(buf.getvalue(), 1000, "jpeg", 80))
    assert info is not None
    assert info.mime == "image/jpeg"


# -- COVERS-RESIZE-8: capabilities and registration --------------------------------------------


def test_COVERS_RESIZE_8_needs_art_and_move() -> None:
    req = CoversResizeStep.requires
    assert req.art
    assert req.move
    assert req.apply
    missing = BackendCapabilities(query=True, apply=True).missing(req)
    assert set(missing) == {"art", "move"}


def test_COVERS_RESIZE_8_registered_through_entry_points() -> None:
    from rifferr.steps.registry import load_step_types

    assert "covers.resize" in load_step_types().names()
    resizers, errors = load_resizers()
    assert "pillow" in resizers
    assert errors == []
