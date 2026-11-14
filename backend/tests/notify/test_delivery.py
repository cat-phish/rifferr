"""S-NOTIFY delivery: retries, redaction, and the Apprise notifier (no network: a custom
Apprise schema stands in for a service)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import apprise
import pytest
from apprise import decorators

from rifferr.core.notify import Notification, NotifySettings, SendResult
from rifferr.core.profile import Secret
from rifferr.notify import ProfileTargets, deliver
from rifferr.notify.apprise import AppriseNotifier

SENT: list[dict[str, Any]] = []


@decorators.notify(on="rifferrtest")
def _fake_service(body: str, title: str, notify_type: str, meta: dict[str, Any], **_: Any) -> bool:
    SENT.append({"body": body, "title": title, "type": notify_type, "meta": meta})
    if meta.get("host") == "fail":
        logging.getLogger("apprise").warning(
            "Failed to reach %s with password %s", meta.get("url"), meta.get("password")
        )
        return False
    return True


@pytest.fixture(autouse=True)
def _clear() -> None:
    SENT.clear()


N = Notification(title="T", body="B", type="failure", url="http://h/runs/r1")


class FlakyNotifier:
    def __init__(self, fail_times: int, error: str = "boom") -> None:
        self.fail_times = fail_times
        self.calls = 0
        self.error = error

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return SendResult(target=target, ok=False, error=self.error)
        return SendResult(target=target, ok=True)


class Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve(self, name: str) -> Secret:
        if name not in self.values:
            raise LookupError(f"no secret named '{name}'")
        return Secret(self.values[name])


async def test_NOTIFY_2_apprise_sends_title_body_type_and_link() -> None:
    result = await AppriseNotifier().send("phone", Secret("rifferrtest://u:sekrit@ok/x"), N)
    assert result == SendResult(target="phone", ok=True)
    (sent,) = SENT
    assert sent["title"] == "T"
    assert sent["body"] == "B\nhttp://h/runs/r1"
    assert sent["type"] == "failure"


async def test_NOTIFY_2_several_urls_in_one_secret() -> None:
    urls = Secret("rifferrtest://u:a@ok/1, rifferrtest://u:b@ok/2")
    assert (await AppriseNotifier().send("all", urls, N)).ok
    assert len(SENT) == 2


async def test_NOTIFY_3_failure_error_never_contains_the_url() -> None:
    result = await AppriseNotifier().send("phone", Secret("rifferrtest://u:sekrit99@fail/x"), N)
    assert not result.ok
    assert result.error
    assert "sekrit99" not in result.error
    assert "rifferrtest://" not in result.error


async def test_NOTIFY_3_unknown_scheme_error_names_no_url() -> None:
    result = await AppriseNotifier().send("phone", Secret("nosuch://tok3n@x"), N)
    assert not result.ok
    assert result.error is not None
    assert "tok3n" not in result.error


def test_NOTIFY_3_apprise_logs_do_not_reach_rifferr_logs() -> None:
    assert logging.getLogger("apprise").propagate is False


async def test_NOTIFY_3_apprise_log_lines_are_redacted() -> None:
    # What reaches Rifferr's (root) handlers: only our redacted copies of Apprise's lines.
    # (pytest's caplog attaches to non-propagating loggers too, so use a root handler.)
    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    root = logging.getLogger()
    handler, old_level = Collect(level=logging.DEBUG), root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        await AppriseNotifier().send("phone", Secret("rifferrtest://u:sekrit77@fail/x"), N)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    assert any("Failed to reach" in r for r in records)
    assert not any("sekrit77" in r for r in records)


async def test_NOTIFY_4_retries_after_each_delay() -> None:
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    flaky = FlakyNotifier(fail_times=2)
    delays = [timedelta(seconds=5), timedelta(seconds=30), timedelta(minutes=2)]
    result = await deliver(flaky, "phone", Secret("x://y"), N, delays, sleep=sleep)
    assert result.ok
    assert flaky.calls == 3
    assert slept == [5.0, 30.0]


async def test_NOTIFY_4_gives_up_after_the_last_delay_and_redacts() -> None:
    async def sleep(s: float) -> None:
        return None

    flaky = FlakyNotifier(fail_times=9, error="could not reach x://supersecret")
    result = await deliver(
        flaky, "phone", Secret("x://supersecret"), N, [timedelta(seconds=1)], sleep=sleep
    )
    assert not result.ok
    assert flaky.calls == 2
    assert result.error == "could not reach ***"


async def test_NOTIFY_5_a_raising_notifier_is_a_failed_send() -> None:
    class Broken:
        async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
            raise RuntimeError("bug")

    result = await deliver(Broken(), "phone", Secret("x://y"), N, [])
    assert result == SendResult(target="phone", ok=False, error="RuntimeError: bug")


async def test_NOTIFY_2_profile_targets_resolve_secrets_when_sending() -> None:
    settings = NotifySettings(targets={"phone": "ntfy"}, retry=[])
    flaky = FlakyNotifier(fail_times=0)
    targets = ProfileTargets(settings, Secrets({"ntfy": "x://y"}), flaky)
    assert targets.names() == ["phone"]
    assert (await targets.send("phone", N)).ok
    missing = await targets.send("pager", N)
    assert missing.error == "unknown target 'pager'"
    gone = ProfileTargets(settings, Secrets({}), flaky)
    result = await gone.send("phone", N)
    assert not result.ok
    assert result.error == "no secret named 'ntfy'"


def test_apprise_types_cover_every_notify_type() -> None:
    from rifferr.notify.apprise import TYPES

    assert set(TYPES) == {"info", "success", "warning", "failure"}
    assert all(isinstance(v, apprise.NotifyType) for v in TYPES.values())
