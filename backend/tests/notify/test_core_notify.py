"""S-NOTIFY pure parts: profile settings, rules, links and message text."""

from __future__ import annotations

import textwrap
from datetime import timedelta
from pathlib import Path

import pytest

from rifferr.core.notify import (
    NO_LINK_HINT,
    NotifyRule,
    NotifySettings,
    QueueRef,
    review_link,
    review_needed,
    run_failed,
    run_link,
    run_summary,
    split_urls,
    worker_problem,
)
from rifferr.core.profile import Profile, ProfileLoadError, parse_profile

BASE = """\
secrets:
  ntfy_phone: env:RIFFERR_NTFY_URL
  discord: keyring:rifferr/discord
"""


def load(extra: str) -> Profile:
    text = BASE + textwrap.dedent(extra)
    return parse_profile(text, "profile.yaml", name="main", root=Path("/p"), env={})


def errors(extra: str) -> list[str]:
    with pytest.raises(ProfileLoadError) as exc:
        load(extra)
    return [str(e) for e in exc.value.errors]


def test_NOTIFY_1_targets_name_secrets() -> None:
    p = load("""\
        notify:
          targets:
            phone: ntfy_phone
            discord: discord
        """)
    assert p.notify.targets == {"phone": "ntfy_phone", "discord": "discord"}


def test_NOTIFY_1_target_with_unknown_secret_is_an_error() -> None:
    errs = errors("""\
        notify:
          targets:
            phone: nope
        """)
    assert any("no secret named 'nope'" in e and "notify.targets.phone" in e for e in errs)


def test_NOTIFY_1_bad_target_name_is_an_error() -> None:
    assert errors("""\
        notify:
          targets:
            Phone!: ntfy_phone
        """)


def test_NOTIFY_1_url_never_in_profile_yaml() -> None:
    # A target maps to a secret *name*; a URL there is just an unknown secret name.
    errs = errors("""\
        notify:
          targets:
            phone: "ntfys://user:pass@ntfy.example/music"
        """)
    assert any("not the URL itself" in e for e in errs)
    assert all("pass" not in e for e in errs)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ntfy://a/b", ["ntfy://a/b"]),
        ("ntfy://a/b discord://x/y", ["ntfy://a/b", "discord://x/y"]),
        (" ntfy://a/b,\n discord://x/y ,", ["ntfy://a/b", "discord://x/y"]),
    ],
)
def test_NOTIFY_1_secret_holds_one_or_more_urls(text: str, expected: list[str]) -> None:
    assert split_urls(text) == expected


def test_NOTIFY_4_retry_is_a_profile_setting() -> None:
    assert NotifySettings().retry_delays() == [
        timedelta(seconds=5),
        timedelta(seconds=30),
        timedelta(minutes=2),
    ]
    p = load("""\
        notify:
          retry: [1m, 1h]
        """)
    assert p.notify.retry_delays() == [timedelta(minutes=1), timedelta(hours=1)]
    assert load("notify:\n  retry: []\n").notify.retry_delays() == []
    assert errors("notify:\n  retry: [soon]\n")


def test_NOTIFY_6_base_url_validated_and_links_built() -> None:
    p = load("notify:\n  base_url: https://rifferr.home.lan/\n")
    assert p.notify.base_url == "https://rifferr.home.lan"
    for bad in ("rifferr.home.lan", "ftp://x", "https://u:p@x", "https://x/?token=1"):
        assert errors(f"notify:\n  base_url: '{bad}'\n"), bad
    base = p.notify.base_url
    assert review_link(base, item="cs1") == "https://rifferr.home.lan/review?item=cs1"
    assert review_link(base, run="r1") == "https://rifferr.home.lan/review?run=r1"
    assert run_link(base, "r1") == "https://rifferr.home.lan/runs/r1"
    assert review_link(None, item="cs1") is None
    assert run_link(None, "r1") is None


def test_NOTIFY_6_no_base_url_no_link_and_a_hint() -> None:
    n = run_failed(
        profile="main", workflow="inbox", run_id="r1", step="s", error="x", base_url=None
    )
    assert n.url is None
    assert NO_LINK_HINT in n.body
    n = run_failed(
        profile="main", workflow="inbox", run_id="r1", step="s", error="x", base_url="http://h"
    )
    assert n.url == "http://h/runs/r1"
    assert NO_LINK_HINT not in n.body


def test_NOTIFY_7_default_rules_only_with_targets() -> None:
    assert NotifySettings().effective_rules() == []
    s = NotifySettings(targets={"phone": "ntfy_phone"})
    (rule,) = s.effective_rules()
    assert rule.on == ["review.needed", "run.failed", "run.summary"]
    assert rule.targets is None
    assert NotifySettings(targets={"phone": "x"}, rules=[]).effective_rules() == []


def test_NOTIFY_7_rules_parse_and_match() -> None:
    p = load("""\
        notify:
          targets: {phone: ntfy_phone, discord: discord}
          rules:
            - on: run.summary
              targets: [discord]
              workflows: [inbox]
            - on: [review.needed, worker.problem]
              remind_after: 1d
        """)
    summary, review = p.notify.effective_rules()
    assert summary.on == ["run.summary"]
    assert summary.matches("run.summary", "inbox")
    assert not summary.matches("run.summary", "other")
    assert not summary.matches("run.failed", "inbox")
    assert review.matches("review.needed", None)
    assert review.remind_after == "1d"


def test_NOTIFY_7_rule_with_unknown_target_or_event_is_an_error() -> None:
    errs = errors("""\
        notify:
          targets: {phone: ntfy_phone}
          rules:
            - on: run.failed
              targets: [pager]
        """)
    assert any("unknown notification target 'pager'" in e for e in errs)
    assert errors("""\
        notify:
          targets: {phone: ntfy_phone}
          rules:
            - on: run.exploded
        """)


def test_NOTIFY_8_review_needed_groups_and_caps_labels() -> None:
    items = [
        QueueRef(kind="import", ref=f"d{i}", labels=(f"Artist {i} - Album",)) for i in range(12)
    ]
    n = review_needed(items, profile="main", workflow="inbox", run_id="r1", base_url="http://h")
    assert n.title == "12 albums need review"
    assert "Artist 9 - Album" in n.body
    assert "Artist 10 - Album" not in n.body
    assert "and 2 more" in n.body
    assert n.url == "http://h/review?run=r1"
    assert "inbox, profile main" in n.body
    one = review_needed(
        items[:1], profile="main", workflow="inbox", run_id="r1", base_url="http://h"
    )
    assert one.url == "http://h/review?item=d0"
    assert one.title == "1 album needs review"


def test_NOTIFY_8_one_gate_for_two_albums_counts_albums() -> None:
    gate = QueueRef(kind="gate", ref="g1", labels=("A - X", "B - Y"))
    n = review_needed([gate], profile="main", workflow="w", run_id="r1", base_url="http://h")
    assert n.title == "2 albums need review"
    assert n.url == "http://h/review?item=g1"


def test_NOTIFY_8_summary_counts_lane_states() -> None:
    n = run_summary(
        {"done": 10, "waiting": 2},
        status="waiting_review",
        profile="main",
        workflow="inbox",
        run_id="r1",
        base_url=None,
    )
    assert "10 done, 2 waiting for review" in n.body
    assert n.type == "warning"
    ok = run_summary(
        {"done": 3}, status="succeeded", profile="main", workflow="inbox", run_id="r", base_url=None
    )
    assert ok.type == "success"


def test_NOTIFY_8_run_failed_names_step_and_first_error_line() -> None:
    n = run_failed(
        profile="main",
        workflow="inbox",
        run_id="r1",
        step="tag",
        error="boom\ntraceback...",
        base_url=None,
    )
    assert n.type == "failure"
    assert "at step 'tag'" in n.body
    assert "boom" in n.body
    assert "traceback" not in n.body


def test_NOTIFY_8_worker_problem_message() -> None:
    n = worker_problem(profile="main", reason="worker_hung", error=None, base_url="http://h")
    assert n.type == "failure"
    assert "stopped answering" in n.body
    assert n.url == "http://h/"


def test_NOTIFY_15_reminder_message() -> None:
    items = [QueueRef(kind="gate", ref="g1", labels=("A - X",))]
    n = review_needed(
        items,
        profile="main",
        workflow="w",
        run_id="r1",
        base_url=None,
        waited=timedelta(days=2),
    )
    assert n.title.startswith("Reminder:")
    assert "waited 2 days" in n.body


def test_NOTIFY_15_remind_limit_must_be_positive() -> None:
    with pytest.raises(ValueError, match="greater than or equal"):
        NotifyRule(on=["review.needed"], remind_limit=0)
