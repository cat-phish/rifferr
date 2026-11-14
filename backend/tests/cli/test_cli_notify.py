"""S-NOTIFY CLI: ``rifferr notify test`` (NOTIFY-14). No server needed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from rifferr.cli import notify as notify_cli
from rifferr.core.notify import Notification, SendResult
from rifferr.core.profile import Secret
from rifferr.profile_store import PROFILE_FILE, config_root, create_profile, profile_dir
from tests.cli.conftest import cli

PROFILE = """\
secrets:
  ntfy: env:RIFFERR_TEST_NTFY
notify:
  targets:
    phone: ntfy
"""


class Fake:
    def __init__(self, ok: bool) -> None:
        self.ok = ok
        self.sent: list[Notification] = []

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        self.sent.append(n)
        error = None if self.ok else f"refused by {urls.reveal()}"
        return SendResult(target=target, ok=self.ok, error=error)


@pytest.fixture(autouse=True)
def profile(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("RIFFERR_TEST_NTFY", "ntfys://u:s3cret@ntfy.example/x")
    create_profile(config_root(), "main")
    (profile_dir(config_root(), "main") / PROFILE_FILE).write_text(PROFILE, encoding="utf-8")
    yield
    notify_cli.NOTIFIER.clear()


def test_NOTIFY_14_cli_sends_a_test(capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake(ok=True)
    notify_cli.NOTIFIER.append(fake)
    assert cli("notify", "test", "phone", "--profile", "main") == 0
    assert "sent a test notification to 'phone'" in capsys.readouterr().out
    assert [n.title for n in fake.sent] == ["Rifferr test notification"]


def test_NOTIFY_14_cli_failure_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    notify_cli.NOTIFIER.append(Fake(ok=False))
    assert cli("notify", "test", "phone", "--profile", "main") == 1
    err = capsys.readouterr().err
    assert "failed" in err
    assert "s3cret" not in err


def test_NOTIFY_14_cli_unknown_target(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli("notify", "test", "pager", "--profile", "main") == 2
    assert "no notification target 'pager' (known: phone)" in capsys.readouterr().err
