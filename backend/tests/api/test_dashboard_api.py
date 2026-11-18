"""F-DASHBOARD routes: inbox, recent imports, import undo, per-album gate decisions,
dashboard events (DASHBOARD-1, -3, -6, -7, -10, -11, -15, -16, -18)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rifferr.api.state import on_all_stream
from rifferr.appdb import AppReviewGates, AppTriggers
from rifferr.backends.base import RUN_MARKER
from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import TargetRef
from rifferr.core.review import GateAlbum
from rifferr.engine.events import Event, profile_stream
from rifferr.profile_store import config_root, profile_dir
from rifferr.triggers import fs
from rifferr.trust import trust_file
from rifferr.worker.readonly import recent_albums
from tests.api.conftest import Api
from tests.api.test_review_queue import gate, post, waiting_run

WATCH = (
    "name: w\non:\n  watch: {path: '{{ vars.inbox }}', settle: 1h}\n"
    "steps:\n  - {id: a, uses: test.echo, with: {message: '{{ run.input }}'}}\n"
)


@pytest.fixture
async def watched(api: Api, tmp_path: Path) -> tuple[Api, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pfile = profile_dir(config_root(), "main") / "profile.yaml"
    pfile.write_text(
        pfile.read_text().replace("vars: {}", f"vars: {{inbox: '{inbox}'}}"), encoding="utf-8"
    )
    trust_file(api.state.db, api.add_workflow("w", WATCH), "test")
    return api, inbox


def album(inbox: Path, name: str) -> Path:
    folder = inbox / name
    folder.mkdir()
    (folder / "01.flac").write_bytes(b"x")
    return folder


async def test_DASHBOARD_1_inbox_route_lists_watched_inputs(watched: tuple[Api, Path]) -> None:
    api, inbox = watched
    album(inbox, "<b>Artist & Co")
    assert api.state.triggers is not None
    await api.state.triggers.reconcile()
    r = await api.client.get("/api/profiles/main/inbox")
    assert r.status_code == 200, r.text
    (folder,) = r.json()
    assert (folder["workflow"], folder["folder"], folder["omitted"]) == ("w", str(inbox), 0)
    (entry,) = folder["entries"]
    # Paths are data; the UI shows them as text (DASHBOARD-16).
    assert (entry["path"], entry["state"]) == ("<b>Artist & Co", "settling")
    assert (await api.client.get("/api/profiles/nosuch/inbox")).status_code == 404


async def test_DASHBOARD_3_import_again_starts_a_manual_run_for_an_undone_input(
    watched: tuple[Api, Path],
) -> None:
    api, inbox = watched
    folder = album(inbox, "A")
    store = AppTriggers(api.state.db)
    fingerprint = fs.input_fingerprint(folder)
    assert fingerprint is not None
    old = waiting_run(api.state.db)
    store.remember("main", "w", str(folder), fingerprint, old.run)
    store.mark_undone(old.run)
    r = await api.client.post(
        "/api/runs", json={"profile": "main", "workflow": "w", "input": str(folder)}
    )
    assert r.status_code == 201, r.text
    await api.wait_finished(r.json()["id"])
    record = store.record("main", "w", str(folder))
    assert record is not None
    assert (record.run_id, record.undone) == (r.json()["id"], False)


def test_DASHBOARD_6_read_only_worker_lists_each_runs_albums() -> None:
    backend = FakeBackend()
    backend.music_dirs = [Path("/music")]
    backend.add_album({"album": "X", "albumartist": "A", RUN_MARKER: "r1", "items": [{}]})
    backend.add_album({"album": "Y", "albumartist": "A", "items": [{}]})
    answer = recent_albums(backend, {"payload": {"run_ids": ["r1", "r2"]}})
    runs: Any = answer["runs"]
    assert [a["label"] for a in runs["r1"]] == ["A - X"]
    assert runs["r2"] == []
    assert answer["library_dirs"] == ["/music"]


async def test_DASHBOARD_6_10_recent_imports_say_what_undo_does_to_files(
    api: Api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = api.state.db
    kept = waiting_run(db)  # no recorded input: files stay
    copied = waiting_run(db)
    empty = waiting_run(db)
    source = album(tmp_path, "src")
    fingerprint = fs.input_fingerprint(source)
    assert fingerprint is not None
    AppTriggers(db).remember("main", "w", str(source), fingerprint, copied.run)
    library = tmp_path / "library"

    def albums(run: str, name: str) -> list[dict[str, Any]]:
        path = library / name
        if name == "C":  # SEC-03: deletion needs a byte-identical copy of the input's files
            path.mkdir(parents=True, exist_ok=True)
            (path / "1.flac").write_bytes(b"x")
        return [{"id": f"a-{name}", "label": name, "path": str(path), "items": [f"{path}/1.flac"]}]

    async def fake_ask(state: Any, profile: str, kind: str, payload: dict[str, Any], **_: Any):  # type: ignore[no-untyped-def]
        assert kind == "imports.recent"
        assert payload["run_ids"] == [empty.run, copied.run, kept.run]  # newest first
        return {
            "runs": {kept.run: albums(kept.run, "K"), copied.run: albums(copied.run, "C")},
            "library_dirs": [str(library)],
        }

    monkeypatch.setattr("rifferr.api.routes.dashboard.ask", fake_ask)
    r = await api.client.get("/api/profiles/main/imports")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [i["run_id"] for i in body] == [copied.run, kept.run]
    (c,) = body[0]["albums"]
    (k,) = body[1]["albums"]
    assert (c["deletes_files"], c["undoable"]) == (True, True)
    assert (k["deletes_files"], k["files_at"]) == (False, str(library / "K"))


async def test_DASHBOARD_7_import_undo_route(api: Api) -> None:
    db = api.state.db
    run = waiting_run(db)
    r = await post(api, f"/api/runs/{run.run}/import-undo", {"albums": ["a1"]})
    assert r.status_code == 202, r.text
    job = db.job(r.json()["job_id"])
    assert job is not None
    assert (job.kind, job.payload["albums"]) == ("import_undo", ["a1"])
    assert (await post(api, "/api/runs/nosuch/import-undo")).status_code == 404
    queued = db.submit("main", {"source": "x", "vars": {}})
    r = await post(api, f"/api/runs/{queued.run}/import-undo")
    assert (r.status_code, r.json()["error"]["code"]) == (409, "not_undoable")
    assert (await post(api, f"/api/runs/{run.run}/import-undo", {"albums": []})).status_code == 400


async def test_DASHBOARD_11_gate_decision_for_some_albums(api: Api) -> None:
    db = api.state.db
    run = waiting_run(db, gates=["g1"])
    two = gate("g1", run.run).model_copy(
        update={
            "albums": [
                GateAlbum(target=TargetRef(scope="album", id="a1"), label="One"),
                GateAlbum(target=TargetRef(scope="album", id="a2"), label="Two"),
            ]
        }
    )
    AppReviewGates(db).add(two)
    # This run has no lanes (suspended the pre-C-LANES way): whole gate only.
    r = await post(api, "/api/reviews/g1/decision", {"decision": "continue", "albums": ["a1"]})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "bad_request")
    r = await post(api, "/api/reviews/g1/decision", {"decision": "continue", "albums": ["zz"]})
    assert r.status_code == 400
    r = await post(
        api, "/api/reviews/g1/decision", {"decision": "continue", "albums": ["a1", "a2"]}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "continued"
    assert body["album_decisions"] == {"a1": "continued", "a2": "continued"}


def test_DASHBOARD_15_dashboard_stream_carries_inbox_and_partial_review_events() -> None:
    from datetime import UTC, datetime

    def event(run_id: str, type_: str) -> Event:
        return Event(seq=1, ts=datetime.now(UTC), run_id=run_id, type=type_, data={})

    assert on_all_stream(event(profile_stream("main"), "inbox.changed"))
    assert on_all_stream(event("01RUN", "review.decided"))
