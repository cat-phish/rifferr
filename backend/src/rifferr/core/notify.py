"""Notifications (S-NOTIFY): profile settings, rule matching, links and message text.

Pure: no network, no Apprise. Delivery lives in ``rifferr.notify``; the server's
rule engine in ``rifferr.notify.service``; the ``notify`` step in ``rifferr.steps``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta
from typing import Annotated, Final, Literal
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

from rifferr.core.duration import Duration, parse_duration

TARGET_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,63}$"
NotifyType = Literal["info", "success", "warning", "failure"]
RuleEvent = Literal[
    "review.needed", "run.failed", "run.summary", "review.timeout", "worker.problem"
]
DEFAULT_RULE_EVENTS: Final[tuple[RuleEvent, ...]] = ("review.needed", "run.failed", "run.summary")
DEFAULT_RETRY: Final = ("5s", "30s", "2m")
#: At most this many album labels in one message; the rest are counted (NOTIFY-8).
MAX_LABELS: Final = 10


class Notification(BaseModel):
    """One message, before it is sent to any target (NOTIFY-2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    body: str
    type: NotifyType = "info"
    url: str | None = None


class SendResult(BaseModel):
    """The final outcome of sending one notification to one target. ``error`` is
    already redacted (NOTIFY-3)."""

    model_config = ConfigDict(frozen=True)

    target: str
    ok: bool
    error: str | None = None


def _one_or_many(value: object) -> object:
    return [value] if isinstance(value, str) else value


TargetName = Annotated[str, Field(pattern=TARGET_NAME_PATTERN)]


class NotifyRule(BaseModel):
    """NOTIFY-7: which rule events go to which targets."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    on: Annotated[list[RuleEvent], BeforeValidator(_one_or_many)] = Field(
        min_length=1, description="Rule events: review.needed, run.failed, run.summary, ..."
    )
    targets: list[TargetName] | None = Field(
        default=None, description="Target names; default: every target"
    )
    workflows: list[str] | None = Field(
        default=None, description="Only runs of these workflows; default: every workflow"
    )
    include_empty: bool = Field(
        default=False, description="run.summary: also for runs that had no albums"
    )
    remind_after: Duration | None = Field(
        default=None,
        description="review.needed: remind every this long while items still wait, e.g. 1d",
    )
    remind_limit: int = Field(default=3, ge=1, description="At most this many reminders")

    def matches(self, event: RuleEvent, workflow: str | None) -> bool:
        if event not in self.on:
            return False
        return self.workflows is None or (workflow is not None and workflow in self.workflows)


class NotifySettings(BaseModel):
    """The ``notify`` section of ``profile.yaml`` (NOTIFY-1, -4, -6, -7)."""

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(
        default=None,
        description="Address of this Rifferr server as the reader opens it, "
        "e.g. https://rifferr.home.lan; links in notifications start with it",
    )
    retry: list[Duration] = Field(
        default_factory=lambda: list(DEFAULT_RETRY),
        description="Delays between attempts when a send fails; [] means no retries",
    )
    targets: dict[TargetName, str] = Field(
        default_factory=dict[str, str],
        description="Target name -> name of the secret holding its Apprise URL(s)",
    )
    rules: list[NotifyRule] | None = Field(
        default=None, description="Rule notifications; omit for the defaults, [] for none"
    )

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an absolute http:// or https:// URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("must not contain a user, password, query or fragment")
        return value.rstrip("/")

    def effective_rules(self) -> list[NotifyRule]:
        """NOTIFY-7: the defaults apply when targets exist and ``rules`` is omitted."""
        if self.rules is not None:
            return list(self.rules)
        if not self.targets:
            return []
        return [NotifyRule(on=list(DEFAULT_RULE_EVENTS))]

    def retry_delays(self) -> list[timedelta]:
        return [parse_duration(d) for d in self.retry]

    def unknown_targets(self, names: Iterable[str]) -> list[str]:
        return [n for n in names if n not in self.targets]

    def checked(self, secret_names: Iterable[str]) -> list[tuple[tuple[str | int, ...], str]]:
        """Problems that need the rest of the profile: (path, message) pairs (NOTIFY-1)."""
        secrets = set(secret_names)
        problems: list[tuple[tuple[str | int, ...], str]] = []
        for name, secret in self.targets.items():
            if "://" in secret or "@" in secret:  # a pasted URL: never repeat it
                problems.append(
                    (
                        ("notify", "targets", name),
                        "must name an entry in secrets that holds the Apprise URL, "
                        "not the URL itself",
                    )
                )
            elif secret not in secrets:
                problems.append(
                    (("notify", "targets", name), f"no secret named '{secret}' in secrets")
                )
        for i, rule in enumerate(self.rules or []):
            for name in self.unknown_targets(rule.targets or []):
                problems.append(
                    (("notify", "rules", i, "targets"), f"unknown notification target '{name}'")
                )
        return problems


def split_urls(text: str) -> list[str]:
    """A target's secret holds one or more Apprise URLs, separated by whitespace or
    commas (NOTIFY-1)."""
    return [u for u in re.split(r"[\s,]+", text.strip()) if u]


# -- links (NOTIFY-6) -------------------------------------------------------------


def review_link(
    base_url: str | None, *, item: str | None = None, run: str | None = None
) -> str | None:
    if base_url is None:
        return None
    if item is not None:
        return f"{base_url}/review?item={quote(item, safe='')}"
    if run is not None:
        return f"{base_url}/review?run={quote(run, safe='')}"
    return f"{base_url}/review"


def run_link(base_url: str | None, run_id: str) -> str | None:
    return None if base_url is None else f"{base_url}/runs/{quote(run_id, safe='')}"


def dashboard_link(base_url: str | None) -> str | None:
    return None if base_url is None else f"{base_url}/"


NO_LINK_HINT: Final = "Open Rifferr to see it."


def _with_hint(body: str, url: str | None) -> str:
    return body if url is not None else f"{body}\n{NO_LINK_HINT}"


# -- rule messages (NOTIFY-8, -10, -15) -----------------------------------------------


class QueueRef(BaseModel):
    """A review-queue item a ``review.needed`` notification is about."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["changeset", "gate", "import", "cleanup", "cover", "move", "prune"]
    ref: str
    labels: tuple[str, ...] = ()


def _where(workflow: str | None, profile: str) -> str:
    return f"{workflow or 'a run'}, profile {profile}"


def _labels(labels: Sequence[str]) -> str:
    shown = list(labels[:MAX_LABELS])
    rest = len(labels) - len(shown)
    text = "\n".join(f"- {label}" for label in shown)
    return text + (f"\n- and {rest} more" if rest else "")


def _albums(n: int) -> str:
    return f"{n} album" + ("" if n == 1 else "s")


def review_needed(
    items: Sequence[QueueRef],
    *,
    profile: str,
    workflow: str | None,
    run_id: str,
    base_url: str | None,
    waited: timedelta | None = None,
) -> Notification:
    """One notification for the queue items a pass created (NOTIFY-8), or a reminder
    for those still pending (NOTIFY-15, ``waited`` set)."""
    labels = list(dict.fromkeys(label for i in items for label in (i.labels or (i.ref,))))
    url = (
        review_link(base_url, item=items[0].ref)
        if len(items) == 1
        else review_link(base_url, run=run_id)
    )
    verb = "needs" if len(labels) == 1 else "need"
    if waited is None:
        title = f"{_albums(len(labels))} {verb} review"
        intro = (
            f"{_albums(len(labels))} {verb} a decision in the review queue "
            f"({_where(workflow, profile)}):"
        )
    else:
        title = f"Reminder: {_albums(len(labels))} still {verb} review"
        intro = (
            f"{_albums(len(labels))} {'has' if len(labels) == 1 else 'have'} waited "
            f"{_human(waited)} in the review queue ({_where(workflow, profile)}):"
        )
    body = f"{intro}\n{_labels(labels)}"
    return Notification(title=title, body=_with_hint(body, url), type="warning", url=url)


def run_failed(
    *,
    profile: str,
    workflow: str | None,
    run_id: str,
    step: str | None,
    error: str | None,
    base_url: str | None,
) -> Notification:
    url = run_link(base_url, run_id)
    first = (error or "").strip().splitlines()[0] if (error or "").strip() else None
    where = f" at step '{step}'" if step else ""
    body = f"The run of {_where(workflow, profile)} failed{where}."
    if first:
        body += f"\n{first}"
    return Notification(
        title=f"Run failed: {workflow or run_id}",
        body=_with_hint(body, url),
        type="failure",
        url=url,
    )


#: Lane states in the order a summary lists them (LANES-2).
SUMMARY_STATES: Final = (
    ("done", "done"),
    ("waiting", "waiting for review"),
    ("failed", "failed"),
    ("rejected", "rejected"),
    ("gone", "gone"),
    ("cancelled", "cancelled"),
)


def run_summary(
    counts: Mapping[str, int],
    *,
    status: str,
    profile: str,
    workflow: str | None,
    run_id: str,
    base_url: str | None,
) -> Notification:
    url = run_link(base_url, run_id)
    parts = [f"{counts[s]} {text}" for s, text in SUMMARY_STATES if counts.get(s)]
    total = sum(counts.values())
    line = ", ".join(parts) if parts else "no albums"
    state = "is waiting for review" if status == "waiting_review" else status
    body = f"The run of {_where(workflow, profile)} {state}: {_albums(total)}.\n{line}"
    kind: NotifyType = {"succeeded": "success", "failed": "failure"}.get(status, "info")  # type: ignore[assignment]
    if status == "waiting_review":
        kind = "warning"
    return Notification(
        title=f"{workflow or 'Run'}: {line}", body=_with_hint(body, url), type=kind, url=url
    )


def review_timeout(
    *,
    what: Literal["gate", "import", "cleanup"],
    ref: str,
    decision: str,
    profile: str,
    workflow: str | None,
    run_id: str,
    base_url: str | None,
) -> Notification:
    url = run_link(base_url, run_id)
    noun = {"gate": "A review gate", "import": "An import decision", "cleanup": "A cleanup"}[what]
    body = (
        f"{noun} in {_where(workflow, profile)} waited too long and was decided "
        f"automatically: {decision}."
    )
    return Notification(
        title=f"Decided by timeout: {decision}", body=_with_hint(body, url), type="warning", url=url
    )


WORKER_REASONS: Final = {
    "worker_crashed": "crashed",
    "worker_hung": "stopped answering and was stopped",
    "worker_failed_to_start": "could not start",
}


def worker_problem(
    *, profile: str, reason: str, error: str | None, base_url: str | None
) -> Notification:
    url = dashboard_link(base_url)
    what = WORKER_REASONS.get(reason, reason)
    body = f"The worker of profile {profile} {what}. Runs it was doing have failed."
    if error and error != reason:
        body += f"\n{error.strip().splitlines()[0]}"
    return Notification(
        title=f"Rifferr worker problem ({profile})",
        body=_with_hint(body, url),
        type="failure",
        url=url,
    )


def test_notification(profile: str, target: str) -> Notification:
    return Notification(
        title="Rifferr test notification",
        body=f"Target '{target}' of profile {profile} works.",
        type="info",
    )


def _human(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}" + ("" if n == 1 else "s")
    return f"{seconds} seconds"
