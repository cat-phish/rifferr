"""The built-in ``Notifier``: Apprise (NOTIFY-2). The only module that imports it."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Final
from urllib.parse import unquote

import apprise

from rifferr.core.notify import Notification, SendResult, split_urls
from rifferr.core.profile import Redactor, Secret

#: Apprise's own log lines can carry URLs with tokens. Keep them out of Rifferr's logs
#: unless redacted (NOTIFY-3): they are captured per send instead.
_apprise_log = logging.getLogger("apprise")
_apprise_log.propagate = False

log = logging.getLogger("rifferr.notify")

TYPES: Final = {
    "info": apprise.NotifyType.INFO,
    "success": apprise.NotifyType.SUCCESS,
    "warning": apprise.NotifyType.WARNING,
    "failure": apprise.NotifyType.FAILURE,
}
TIMEOUT: Final = 30.0


class AppriseNotifier:
    def __init__(self, timeout: float = TIMEOUT) -> None:
        self.timeout = timeout

    async def send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        return await asyncio.to_thread(self._send, target, urls, n)

    def _send(self, target: str, urls: Secret, n: Notification) -> SendResult:
        values = split_urls(urls.reveal())
        redactor = Redactor([urls, *(Secret(u) for u in values), *_parts(values)])
        lines: list[str] = []

        def capture(entry: Any, plugin: Any = None) -> None:
            lines.append(redactor.redact(str(entry)))

        client = apprise.Apprise()
        bad = [i for i, url in enumerate(values, 1) if not client.add(url)]
        if bad:
            which = ", ".join(str(i) for i in bad)
            return SendResult(
                target=target,
                ok=False,
                error=f"Apprise doesn't understand URL {which} of {len(values)} (check the scheme)",
            )
        if not values:
            return SendResult(target=target, ok=False, error="the target's secret holds no URL")
        body = n.body if n.url is None else f"{n.body}\n{n.url}"
        try:
            ok = bool(
                client.notify(
                    body=body,
                    title=n.title,
                    notify_type=TYPES[n.type],
                    body_format=apprise.NotifyFormat.TEXT,  # never HTML or Markdown
                    timeout=self.timeout,
                    log_callback=capture,
                )
            )
        except Exception as exc:
            return SendResult(
                target=target, ok=False, error=redactor.redact(f"{type(exc).__name__}: {exc}")
            )
        for line in lines:
            log.debug("apprise: %s", line)
        if ok:
            return SendResult(target=target, ok=True)
        detail = next((line for line in reversed(lines) if line), "Apprise reported a failure")
        return SendResult(target=target, ok=False, error=_trim(detail))


def _trim(line: str) -> str:
    # "2026-11-14 18:38:07,481 - WARNING - Failed to send ..." -> the message part.
    parts = line.split(" - ", 2)
    return parts[2] if len(parts) == 3 else line


def _parts(urls: list[str]) -> list[Secret]:
    """Pieces of each URL (user, password, host, path segments): Apprise logs them on
    their own, and service tokens often sit in the path."""
    pieces: list[Secret] = []
    for url in urls:
        _, _, rest = url.partition("://")
        for piece in re.split(r"[/:@?&=#,]+", unquote(rest)):
            if piece:
                pieces.append(Secret(piece))
    return pieces
