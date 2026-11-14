"""F-TRIGGERS pure helpers: durations, payload lookup, fingerprints, settle, dedup."""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rifferr.core.triggers import (
    DedupRecord,
    SettleTracker,
    dedup_skip,
    fingerprint,
    lookup,
    parse_duration,
    top_level,
)


@pytest.mark.parametrize(("text", "seconds"), [("5s", 5), ("2m", 120), ("1h", 3600)])
def test_TRIGGERS_9_parse_duration(text: str, seconds: float) -> None:
    assert parse_duration(text) == seconds


@pytest.mark.parametrize("text", ["", "m", "2", "2d", "-1m", "1.5m"])
def test_TRIGGERS_9_parse_duration_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="not a duration"):
        parse_duration(text)


PAYLOAD: dict[str, Any] = {
    "eventType": "Download",
    "trackFiles": [{"path": "/in/A/01.flac"}, {"path": "/in/A/02.flac"}],
    "n": 3,
}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("trackFiles.0.path", "/in/A/01.flac"),
        ("trackFiles.1.path", "/in/A/02.flac"),
        ("eventType", "Download"),
        ("trackFiles.2.path", None),
        ("missing", None),
        ("n.x", None),
        ("trackFiles.x", None),
    ],
)
def test_TRIGGERS_15_lookup_dotted_path(path: str, value: Any) -> None:
    assert lookup(PAYLOAD, path) == value


def test_TRIGGERS_16_lookup_missing_gives_none_for_test_events() -> None:
    assert lookup({"eventType": "Test"}, "trackFiles.0.path") is None
    assert lookup([], "0") is None


def test_TRIGGERS_19_fingerprint_empty_is_none() -> None:
    assert fingerprint([]) is None


def test_TRIGGERS_19_fingerprint_changes_with_any_field() -> None:
    base = [("a/01.flac", 10, 1), ("a/02.flac", 20, 2)]
    fp = fingerprint(base)
    assert fp == fingerprint(list(reversed(base)))
    assert fp != fingerprint([("a/01.flac", 11, 1), ("a/02.flac", 20, 2)])
    assert fp != fingerprint([("a/01.flac", 10, 9), ("a/02.flac", 20, 2)])
    assert fp != fingerprint([("a/01.flac", 10, 1), ("a/03.flac", 20, 2)])
    assert fp != fingerprint(base[:1])


@given(st.lists(st.tuples(st.text(), st.integers(0), st.integers(0)), min_size=1))
def test_TRIGGERS_19_fingerprint_is_order_independent(files: list[tuple[str, int, int]]) -> None:
    assert fingerprint(files) == fingerprint(list(reversed(files)))


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("queued", "in_progress"),
        ("running", "in_progress"),
        ("waiting_review", "in_progress"),
        ("succeeded", "already_run"),
        ("failed", "previously_failed"),
        ("cancelled", "previously_failed"),
        (None, None),
    ],
)
def test_TRIGGERS_19_same_fingerprint_is_skipped(state: str | None, reason: str | None) -> None:
    assert dedup_skip(DedupRecord("fp", "r1", state), "fp") == reason


@pytest.mark.parametrize("state", ["succeeded", "failed", "running"])
def test_TRIGGERS_20_changed_fingerprint_runs_again(state: str) -> None:
    assert dedup_skip(DedupRecord("old", "r1", state), "new") is None


def test_TRIGGERS_20_force_bypasses_dedup() -> None:
    assert dedup_skip(DedupRecord("fp", "r1", "succeeded"), "fp", force=True) is None


def test_TRIGGERS_19_no_record_or_empty_input_runs() -> None:
    assert dedup_skip(None, "fp") is None
    assert dedup_skip(DedupRecord("fp", "r1", "succeeded"), None) is None


def test_TRIGGERS_9_settle_waits_for_quiet() -> None:
    t = SettleTracker(settle=120)
    t.touch("A", 0)
    t.touch("B", 30)
    assert t.next_deadline() == 120
    assert t.due(119) == []
    t.touch("A", 100)  # still changing
    assert t.due(150) == ["B"]
    assert t.pending() == ["A"]
    assert t.due(219) == []
    assert t.due(220) == ["A"]
    assert t.next_deadline() is None


def test_TRIGGERS_9_discarded_candidate_never_fires() -> None:
    t = SettleTracker(settle=10)
    t.touch("A", 0)
    t.discard("A")
    assert t.due(100) == []


@pytest.mark.parametrize(
    ("rel", "top"),
    [("A/01.flac", "A"), ("A", "A"), ("A/cd1/01.flac", "A"), ("", None), (".", None)],
)
def test_TRIGGERS_8_top_level_entry(rel: str, top: str | None) -> None:
    assert top_level(rel) == top
