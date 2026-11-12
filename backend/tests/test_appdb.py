from __future__ import annotations

import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rifferr.appdb import AppDB
from rifferr.engine import Event
from rifferr.profile_store import app_db_path, data_root


def payload() -> dict[str, object]:
    return {"source": "name: t\nsteps: []\n"}


def test_WORKER_3_app_db_is_a_private_sqlite_file(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "d" / "rifferr.db")
    assert stat.S_IMODE((tmp_path / "d" / "rifferr.db").stat().st_mode) == 0o600
    assert db.path.read_bytes().startswith(b"SQLite format 3")


def test_WORKER_3_data_root_precedence(tmp_path: Path) -> None:
    assert data_root({"RIFFERR_DATA_DIR": "/d", "RIFFERR_CONFIG_DIR": "/c"}) == Path("/d")
    assert data_root({"RIFFERR_CONFIG_DIR": "/config"}) == Path("/config")
    assert data_root({"XDG_DATA_HOME": "/x"}) == Path("/x/rifferr")
    assert data_root({"HOME": "/h"}) == Path("/h/.local/share/rifferr")
    assert app_db_path({"RIFFERR_DATA_DIR": "/d"}) == Path("/d/rifferr.db")


def test_WORKER_3_claim_is_oldest_first_and_per_profile(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "a.db")
    a = db.submit("main", payload())  # type: ignore[arg-type]
    db.submit("other", payload())  # type: ignore[arg-type]
    b = db.submit("main", payload())  # type: ignore[arg-type]
    claimed = db.claim_next("main")
    assert claimed is not None
    assert (claimed.run_id, claimed.state) == (a.run_id, "running")
    second = db.claim_next("main")
    assert second is not None
    assert second.run_id == b.run_id
    assert db.claim_next("main") is None


def test_WORKER_3_claim_is_exclusive_across_connections(tmp_path: Path) -> None:
    one, two = AppDB(tmp_path / "a.db"), AppDB(tmp_path / "a.db")
    one.submit("main", payload())  # type: ignore[arg-type]
    claims = [one.claim_next("main"), two.claim_next("main")]
    assert sum(c is not None for c in claims) == 1


def test_WORKER_3_finish_and_cancel_only_from_the_right_state(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "a.db")
    job = db.submit("main", payload())  # type: ignore[arg-type]
    assert not db.finish(job.run_id, "succeeded")  # not running yet
    assert db.cancel_queued(job.run_id)
    assert not db.cancel_queued(job.run_id)
    assert db.claim_next("main") is None


def test_WORKER_4_event_store_enforces_sequence(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "a.db")
    now = datetime(2026, 11, 11, tzinfo=UTC)
    e1 = Event(seq=1, ts=now, run_id="r", type="run.started", data={"x": [1, "a"]})
    db.append_event(e1)
    with pytest.raises(ValueError, match="expected seq 2"):
        db.append_event(Event(seq=3, ts=now, run_id="r", type="run.finished"))
    assert db.events_since("r", 0) == [e1]
    assert db.last_seq("r") == 1
