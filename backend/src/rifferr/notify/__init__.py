"""Sending notifications (S-NOTIFY NOTIFY-2 to -5).

``Notifier`` makes one attempt to send to one target; ``deliver`` adds the profile's
retries; ``ProfileTargets`` resolves a target's Apprise URLs from the profile's secrets
at the point of sending. Only ``rifferr.notify.apprise`` imports Apprise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import Protocol

from rifferr.core.notify import Notification, NotifySettings, SendResult
from rifferr.core.profile import Redactor, Secret

log = logging.getLogger("rifferr.notify")

Sleep = Callable[[float], Awaitable[None]]


class Notifier(Protocol):
    """One attempt to send ``n`` to the Apprise URL(s) in ``urls``. Must not raise for a
    failed send; ``SendResult.error`` must not contain the URLs (NOTIFY-3)."""

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult: ...


class SecretSource(Protocol):
    def resolve(self, name: str) -> Secret:
        """Raise ``LookupError`` if unknown or unavailable; never include the value."""
        ...


async def deliver(
    notifier: Notifier,
    target: str,
    urls: Secret,
    n: Notification,
    delays: Sequence[timedelta],
    *,
    sleep: Sleep = asyncio.sleep,
    on_attempt: Callable[[int], None] | None = None,
) -> SendResult:
    """Send with retries after each of ``delays`` (NOTIFY-4)."""
    redactor = Redactor([urls])
    result = SendResult(target=target, ok=False, error="not sent")
    for attempt in range(len(delays) + 1):
        if attempt:
            await sleep(delays[attempt - 1].total_seconds())
        if on_attempt is not None:
            on_attempt(attempt + 1)
        try:
            result = await notifier.send(target, urls, n)
        except Exception as exc:  # a notifier bug must not break a run (NOTIFY-5)
            result = SendResult(target=target, ok=False, error=f"{type(exc).__name__}: {exc}")
        if result.error is not None:
            result = result.model_copy(update={"error": redactor.redact(result.error)})
        if result.ok:
            return result
        log.info("notification to %s failed (attempt %d): %s", target, attempt + 1, result.error)
    return result


class ProfileTargets:
    """A profile's notification targets (NOTIFY-1): names, and the URLs behind them,
    resolved only when sending (PROFILE-5)."""

    def __init__(
        self,
        settings: NotifySettings,
        secrets: SecretSource,
        notifier: Notifier,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self._secrets = secrets
        self._notifier = notifier
        self._sleep = sleep

    @property
    def base_url(self) -> str | None:
        return self.settings.base_url

    def names(self) -> list[str]:
        return list(self.settings.targets)

    async def send(
        self,
        target: str,
        n: Notification,
        *,
        retry: bool = True,
        on_attempt: Callable[[int], None] | None = None,
    ) -> SendResult:
        secret_name = self.settings.targets.get(target)
        if secret_name is None:
            return SendResult(target=target, ok=False, error=f"unknown target '{target}'")
        try:
            urls = self._secrets.resolve(secret_name)
        except LookupError as exc:
            return SendResult(target=target, ok=False, error=str(exc))
        delays = self.settings.retry_delays() if retry else []
        return await deliver(
            self._notifier, target, urls, n, delays, sleep=self._sleep, on_attempt=on_attempt
        )


def default_notifier() -> Notifier:
    from rifferr.notify.apprise import AppriseNotifier

    return AppriseNotifier()


__all__ = ["Notifier", "ProfileTargets", "SecretSource", "default_notifier", "deliver"]
