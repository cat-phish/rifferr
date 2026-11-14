"""``rifferr webhook new|rotate|list`` (F-TRIGGERS TRIGGERS-17). No server needed.

Secrets are shown once and only their SHA-256 is stored in the app DB.
"""

from __future__ import annotations

import argparse
import re
import sys

from rifferr.appdb import AppDB, AppTriggers
from rifferr.cli.files import EXIT_OK, EXIT_USAGE
from rifferr.core.profile import ProfileError, ProfileLoadError
from rifferr.core.workflow.models import NAME_PATTERN
from rifferr.profile_store import app_db_path, config_root, load_profile
from rifferr.triggers.secrets import new_secret


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("webhook", help="create and list webhook secrets")
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")
    for action, help_ in (
        ("new", "create a webhook's secret (shown once)"),
        ("rotate", "replace a webhook's secret; the old one stops working"),
    ):
        a = actions.add_parser(action, help=help_)
        a.add_argument("name", help="the workflow's on.webhook.name")
        a.add_argument("--profile", required=True)
        a.set_defaults(func=cmd_secret)
    ls = actions.add_parser("list", help="list webhooks that have a secret")
    ls.add_argument("--profile")
    ls.set_defaults(func=cmd_list)


def cmd_secret(args: argparse.Namespace) -> int:
    prog = f"rifferr webhook {args.action}"
    if not re.match(NAME_PATTERN, args.name):
        print(f"{prog}: invalid webhook name {args.name!r}", file=sys.stderr)
        return EXIT_USAGE
    try:
        load_profile(config_root(), args.profile)
    except (ProfileError, ProfileLoadError) as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    db = AppDB(app_db_path())
    try:
        store = AppTriggers(db)
        exists = store.webhook_hash(args.profile, args.name) is not None
        if args.action == "new" and exists:
            print(
                f"{prog}: webhook '{args.name}' already has a secret; "
                "use 'rifferr webhook rotate' to replace it",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if args.action == "rotate" and not exists:
            print(f"{prog}: webhook '{args.name}' has no secret yet; use 'new'", file=sys.stderr)
            return EXIT_USAGE
        secret = new_secret(store, args.profile, args.name)
    finally:
        db.close()
    print(f"webhook: POST /hooks/{args.profile}/{args.name}")
    print(f"secret:  {secret}")
    print(
        "Send it as 'Authorization: Bearer <secret>' or as the password of HTTP Basic auth.\n"
        "It is shown only now; Rifferr keeps only a hash of it."
    )
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    db = AppDB(app_db_path())
    try:
        rows = AppTriggers(db).webhooks(args.profile)
    finally:
        db.close()
    for profile, name, created in rows:
        print(f"{profile}\t{name}\t/hooks/{profile}/{name}\tsecret since {created:%Y-%m-%d %H:%M}")
    if not rows:
        print("no webhook secrets", file=sys.stderr)
    return EXIT_OK
