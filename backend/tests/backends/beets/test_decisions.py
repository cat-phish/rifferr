"""F-IMPORT-UI against a real beets library: lookup with deferred decisions, applying a
decision, and looking a folder up again. Contract tests for the beets ImportSession hooks
we use (``choose_match``, ``get_duplicate_action``, ``search_ids``, ``tag_album``).

Candidates come from a fixture metadata-source plugin (``beetsplug/rifferrfixture.py``),
so nothing talks to MusicBrainz.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rifferr.backends.beets import BeetsBackend, config
from rifferr.core.imports import ImportApplyError
from rifferr.core.selection import Selection
from tests.backends.beets.conftest import BASE_CONFIG, BeetsEnv

RUN = "01RUNIMPORTUI000000000001"
PLUGINS = Path(__file__).parent / "beetsplug"

TRACKS = [{"title": "First", "length": 0.5}, {"title": "Second", "length": 0.5}]
EXACT = {
    "album_id": "exact",
    "album": "Fixture Album",
    "artist": "Fixture Artist",
    "tracks": TRACKS,
}
OTHER = {
    "album_id": "other",
    "album": "Something Else",
    "artist": "Nobody",
    "tracks": [{"title": "Zzz", "length": 300.0}, {"title": "Yyy", "length": 300.0}],
}
BY_ID = {**EXACT, "album_id": "by-id", "album": "Fixture Album (Deluxe)", "search": False}


def backend(env: BeetsEnv, releases: list[dict[str, Any]], extra: str = "") -> BeetsBackend:
    data = env.root / "releases.json"
    data.write_text(json.dumps(releases))
    import os

    os.environ["RIFFERR_FIXTURE_RELEASES"] = str(data)
    text = BASE_CONFIG.format(
        music=env.music, db=env.beetsdir / "library.db", log=env.beetsdir / "import.log"
    )
    text = text.replace("plugins: []", f"pluginpath: ['{PLUGINS}']\nplugins: [rifferrfixture]")
    text = text.replace("autotag: no", "autotag: yes")
    (env.beetsdir / "config.yaml").write_text(text + extra)
    config.load(env.beetsdir)
    return BeetsBackend(env.beetsdir.resolve())


@pytest.fixture(autouse=True)
def _releases_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RIFFERR_FIXTURE_RELEASES", raising=False)


def test_IMPORT_UI_1_auto_imports_a_strong_match(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [EXACT])
    beets_env.inbox_album()
    result = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN)
    assert [a.fields["album"] for a in result.imported] == ["Fixture Album"]
    assert result.questions == []


def test_IMPORT_UI_4_weak_match_becomes_a_question_and_stays_in_place(
    beets_env: BeetsEnv,
) -> None:
    b = backend(beets_env, [OTHER])
    folder = beets_env.inbox_album()
    result = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN)
    assert result.imported == []
    (q,) = result.questions
    assert (q.kind, q.path) == ("match", str(folder))
    assert q.fingerprint
    assert [c.id for c in q.candidates] == ["RifferrFixture:other"]
    (c,) = q.candidates
    assert (c.source, c.album, c.tracks, c.url) == (
        "RifferrFixture",
        "Something Else",
        2,
        "https://fixture.invalid/release/other",
    )
    assert 0 < c.distance <= 1
    assert c.penalties
    assert sorted(m.title_after or "" for m in c.mapping) == ["Yyy", "Zzz"]
    assert sorted(str(i.tags.get("title")) for i in q.items) == ["First", "Second"]
    assert sorted(p.name for p in folder.iterdir()) == ["01 First.flac", "02 Second.flac"]
    assert b.query(Selection(run_id=RUN)) == []


def test_IMPORT_UI_1_interactive_asks_even_for_a_strong_match(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [EXACT])
    beets_env.inbox_album()
    result = b.import_folder(beets_env.inbox, "interactive", None, run_id=RUN)
    assert result.imported == []
    assert [c.id for c in result.questions[0].candidates] == ["RifferrFixture:exact"]


def test_IMPORT_UI_1_timid_makes_auto_ask(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [EXACT])
    with config.overlay({"import": {"timid": True}}):
        beets_env.inbox_album()
        result = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN)
    assert result.imported == []
    assert len(result.questions) == 1


def test_IMPORT_UI_11_apply_a_candidate_by_id(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [OTHER, EXACT | {"search": False}])
    beets_env.inbox_album()
    (q,) = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN).questions
    result = b.import_decided(
        Path(q.path),
        "candidate",
        fingerprint=q.fingerprint,
        candidate_id="RifferrFixture:exact",
        run_id=RUN,
        set_fields={"source": "decided"},
    )
    (album,) = result.imported
    assert album.fields["album"] == "Fixture Album"
    assert album.fields["rifferr_run"] == RUN
    assert album.fields["source"] == "decided"


def test_IMPORT_UI_11_asis_keeps_current_tags(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [OTHER])
    beets_env.inbox_album()
    (q,) = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN).questions
    (album,) = b.import_decided(
        Path(q.path), "asis", fingerprint=q.fingerprint, run_id=RUN
    ).imported
    assert album.fields["album"] == "Fixture Album"


def test_IMPORT_UI_11_candidate_not_found_again_or_changed_folder_raise(
    beets_env: BeetsEnv,
) -> None:
    b = backend(beets_env, [OTHER])
    folder = beets_env.inbox_album()
    (q,) = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN).questions
    with pytest.raises(ImportApplyError, match="not found again"):
        b.import_decided(
            folder, "candidate", fingerprint=q.fingerprint, candidate_id="RifferrFixture:gone"
        )
    (folder / "extra.txt").write_text("new file")
    with pytest.raises(ImportApplyError, match="folder changed"):
        b.import_decided(folder, "asis", fingerprint=q.fingerprint)
    assert b.query(Selection()) == []


def test_IMPORT_UI_9_lookup_by_id_and_by_name(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [OTHER, BY_ID])
    folder = beets_env.inbox_album()
    plain = b.lookup(folder)
    assert [c.id for c in plain.candidates] == ["RifferrFixture:other"]
    assert plain.fingerprint
    assert len(plain.items) == 2
    assert [c.id for c in b.lookup(folder, ids=["by-id"]).candidates] == ["RifferrFixture:by-id"]
    by_name = b.lookup(folder, artist="Nobody", album="Something Else")
    assert "RifferrFixture:other" in [c.id for c in by_name.candidates]
    assert b.query(Selection()) == []  # a lookup imports nothing


def test_IMPORT_UI_3_duplicate_with_ask_becomes_a_question(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [EXACT])
    beets_env.inbox_album(name="first")
    b.import_folder(beets_env.inbox, "auto", None, run_id=RUN)
    second = beets_env.root / "second"
    second.mkdir()
    import shutil

    from tests.backends.beets.conftest import FIXTURES

    shutil.copytree(FIXTURES / "album-flac", second / "album-flac")
    result = b.import_folder(
        second,
        "auto",
        None,
        run_id="01RUNIMPORTUI000000000002",
        options={"duplicate_action": "ask"},
    )
    (q,) = result.questions
    assert q.kind == "duplicate"
    assert [d.fields["album"] for d in q.duplicates] == ["Fixture Album"]
    assert [c.id for c in q.candidates] == ["RifferrFixture:exact"]
    (album,) = b.import_decided(
        Path(q.path),
        "keep",
        fingerprint=q.fingerprint,
        candidate_id="RifferrFixture:exact",
        run_id="01RUNIMPORTUI000000000002",
    ).imported
    assert len(b.query(Selection())) == 2
    del album


NEAR = {**EXACT, "album_id": "near", "album": "Fixture Album Live"}
WEAK_ONLY = "match:\n  strong_rec_thresh: 0.0\n"


def test_IMPORT_UI_2_candidates_keep_beets_order(beets_env: BeetsEnv) -> None:
    b = backend(beets_env, [OTHER, NEAR], WEAK_ONLY)
    beets_env.inbox_album()
    (q,) = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN).questions
    # beets' order (closest first), nothing filtered or rescored.
    assert [c.id for c in q.candidates] == ["RifferrFixture:near", "RifferrFixture:other"]
    assert q.candidates[0].distance <= q.candidates[1].distance


def test_IMPORT_UI_2_candidates_follow_the_users_match_config(beets_env: BeetsEnv) -> None:
    # match.ignored drops candidates with an artist penalty: beets filters, not Rifferr.
    b = backend(beets_env, [OTHER, NEAR], WEAK_ONLY + "  ignored: [artist]\n")
    beets_env.inbox_album()
    (q,) = b.import_folder(beets_env.inbox, "auto", None, run_id=RUN).questions
    assert [c.id for c in q.candidates] == ["RifferrFixture:near"]
