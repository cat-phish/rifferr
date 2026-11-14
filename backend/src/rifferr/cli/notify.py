"""``rifferr notify test <target> --profile`` (S-NOTIFY NOTIFY-14). No server needed:
it resolves the target's secret and sends one test notification from this process."""

from __future__ import annotations

import argparse
import asyncio
import sys

from rifferr.cli.files import EXIT_OK, EXIT_USAGE
from rifferr.core.notify import test_notification
from rifferr.core.profile import ProfileError, ProfileLoadError
from rifferr.credentials import ProfileSecrets, SecretResolver
from rifferr.notify import Notifier, ProfileTargets, default_notifier
from rifferr.profile_store import config_root, load_profile

EXIT_FAILED = 1

#: Tests swap in a fake; the default is Apprise.
NOTIFIER: list[Notifier] = []


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("notify", help="notification targets")
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    t = actions.add_parser("test", help="send a test notification to a target")
    t.add_argument("target", help="a name from the profile's notify.targets")
    t.add_argument("--profile", required=True)
    t.set_defaults(func=cmd_test)


def cmd_test(args: argparse.Namespace) -> int:
    prog = "rifferr notify test"
    try:
        profile = load_profile(config_root(), args.profile)
    except (ProfileError, ProfileLoadError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.target not in profile.notify.targets:
        known = ", ".join(sorted(profile.notify.targets)) or "none"
        print(f"{prog}: no notification target '{args.target}' (known: {known})", file=sys.stderr)
        return EXIT_USAGE
    notifier = NOTIFIER[0] if NOTIFIER else default_notifier()
    targets = ProfileTargets(
        profile.notify, ProfileSecrets(profile.secrets, SecretResolver()), notifier
    )
    n = test_notification(profile.name, args.target)
    result = asyncio.run(targets.send(args.target, n, retry=False))
    if result.ok:
        print(f"sent a test notification to '{args.target}'")
        return EXIT_OK
    print(f"{prog}: sending to '{args.target}' failed: {result.error}", file=sys.stderr)
    return EXIT_FAILED
