"""S-COVERS-RESIZE with beets: the original is kept next to the cover, the cover shrinks,
and undo brings it back (temp profile, real files)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from rifferr.artstore import ArtStore
from rifferr.backends.base import RUN_MARKER
from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange, TargetRef
from rifferr.core.covers import ART_FIELD, sniff_image
from rifferr.core.selection import Selection
from rifferr.engine import RunState, StepTypeRegistry
from rifferr.engine.undo import UndoContext, undo_entry
from rifferr.steps.covers_resize import CoversResizeStep
from tests.backends.beets.conftest import BeetsEnv
from tests.engine.conftest import Harness
from tests.engine.steps import ALL


def jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def size(path: Path) -> tuple[int, int]:
    info = sniff_image(path.read_bytes())
    assert info is not None
    return info.width, info.height


async def test_COVERS_RESIZE_1_COVERS_RESIZE_5_beets_keeps_original_and_undoes(
    beets_env: BeetsEnv,
) -> None:
    backend = beets_env.backend(art=True)
    store = ArtStore(beets_env.root / "art")
    folder = beets_env.inbox_album()
    backend.import_folder(folder, "quiet", None, run_id="x", options={"copy": False, "move": False})
    h = Harness()
    h.backend = backend  # type: ignore[assignment]
    h.engine.registry = StepTypeRegistry([*ALL, CoversResizeStep(art_root=store.root)])
    run = h.run(
        "name: art\nsteps:\n  - id: art-size\n    uses: covers.resize\n    with: {max: 1200}\n"
    )
    with backend.library() as lib:
        [album] = lib.albums()
        album[RUN_MARKER] = run.id
        album.store()
    [snap] = backend.query(Selection())
    big = jpeg(2400, 1600)
    sha, _ = store.put(big)
    ref = TargetRef(scope="album", id=snap.id)
    set_art = ChangeSet(
        id="cs-art",
        profile="main",
        source=ChangeSource(kind="manual", name="test"),
        created_at=datetime(2026, 11, 22, tzinfo=UTC),
        targets=[TargetChange(target=ref, changes=[FieldChange(field=ART_FIELD, new=sha)])],
    )
    assert backend.apply(set_art).applied
    cover = Path(str(backend.query(Selection())[0].fields["artpath"]))
    assert size(cover) == (2400, 1600)

    await h.engine.execute(run)
    assert run.state is RunState.succeeded, h.logs(run, "error")
    kept = cover.with_name(f"{cover.stem}-hires{cover.suffix}")
    assert kept.read_bytes() == big
    now = Path(str(backend.query(Selection())[0].fields["artpath"]))
    assert now == cover
    assert size(now) == (1200, 800)

    [entry] = h.engine.journal.for_run(run.id)
    outcome = undo_entry(
        entry.id,
        UndoContext(
            backend=backend,
            journal=h.engine.journal,
            store=h.engine.changesets,
            new_id=lambda: "undo-1",
            now=h.clock.now,
            emitter_for=lambda run_id, step: lambda type, data: None,
            by="tester",
        ),
    )
    assert outcome.undone, outcome
    assert not kept.exists()
    restored = Path(str(backend.query(Selection())[0].fields["artpath"]))
    assert restored.read_bytes() == big


async def test_COVERS_RESIZE_1_the_albums_own_image_becomes_cover_hires(
    beets_env: BeetsEnv,
) -> None:
    backend = beets_env.backend(art=True)
    store = ArtStore(beets_env.root / "art")
    folder = beets_env.inbox_album()
    backend.import_folder(folder, "quiet", None, run_id="x", options={"copy": False, "move": False})
    h = Harness()
    h.backend = backend  # type: ignore[assignment]
    h.engine.registry = StepTypeRegistry([*ALL, CoversResizeStep(art_root=store.root)])
    run = h.run(
        "name: art\nsteps:\n  - id: art-size\n    uses: covers.resize\n    with: {max: 1200}\n"
    )
    with backend.library() as lib:
        [album] = lib.albums()
        album[RUN_MARKER] = run.id
        album.store()
    [snap] = backend.query(Selection())
    own = Path(snap.path or "") / "folder.jpg"
    big = jpeg(2400, 1600)
    own.write_bytes(big)
    sha, _ = store.put(big)
    ref = TargetRef(scope="album", id=snap.id)
    adopt = ChangeSet(
        id="cs-art",
        profile="main",
        source=ChangeSource(kind="manual", name="test"),
        created_at=datetime(2026, 11, 22, tzinfo=UTC),
        targets=[TargetChange(target=ref, changes=[FieldChange(field=ART_FIELD, new=sha)])],
    )
    assert backend.apply(adopt).applied
    assert backend.query(Selection())[0].fields["artpath"] == str(own.with_name("cover.jpg"))

    await h.engine.execute(run)
    assert run.state is RunState.succeeded, h.logs(run, "error")
    images = sorted(p.name for p in own.parent.iterdir() if p.suffix == ".jpg")
    assert images == ["cover-hires.jpg", "cover.jpg"]  # no folder.jpg duplicate
    assert (own.parent / "cover-hires.jpg").read_bytes() == big
    assert size(own.parent / "cover.jpg") == (1200, 800)

    [entry] = h.engine.journal.for_run(run.id)
    outcome = undo_entry(
        entry.id,
        UndoContext(
            backend=backend,
            journal=h.engine.journal,
            store=h.engine.changesets,
            new_id=lambda: "undo-1",
            now=h.clock.now,
            emitter_for=lambda run_id, step: lambda type, data: None,
            by="tester",
        ),
    )
    assert outcome.undone, outcome
    restored = Path(str(backend.query(Selection())[0].fields["artpath"]))
    assert restored.read_bytes() == big
