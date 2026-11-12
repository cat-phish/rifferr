"""``rifferr profile create|list`` (CLI-13)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rifferr.core.profile import ProfileError, ProfileLoadError
from rifferr.profile_store import config_root, create_profile, list_profiles, load_profile


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser("profile", help="manage profiles")
    psub = p.add_subparsers(dest="profile_command", required=True, metavar="COMMAND")
    c = psub.add_parser("create", help="create an empty profile")
    c.add_argument("name")
    c.add_argument("--beetsdir", type=Path, help="beets config directory (BEETSDIR)")
    c.set_defaults(func=cmd_create)
    ls = psub.add_parser("list", help="list profiles")
    ls.set_defaults(func=cmd_list)


def cmd_create(args: argparse.Namespace) -> int:
    try:
        path = create_profile(config_root(), args.name, args.beetsdir)
    except ProfileError as exc:
        print(f"rifferr profile create: {exc}", file=sys.stderr)
        return 1
    print(f"created profile '{args.name}' at {path}")
    if args.beetsdir is None:
        print(f"set beets.beetsdir in {path / 'profile.yaml'} before running beets steps")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    root = config_root()
    for name in list_profiles(root):
        try:
            profile = load_profile(root, name)
        except (ProfileError, ProfileLoadError) as exc:
            print(f"{name}\terror: {exc}")
            continue
        print(f"{name}\t{profile.backend}")
    return 0
