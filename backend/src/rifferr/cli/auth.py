"""``rifferr passwd`` and ``rifferr sign-out`` (F-SECURITY SECURITY-16, -17)."""

from __future__ import annotations

import argparse
import getpass
import sys

from rifferr.appdb import AppDB
from rifferr.passwords import DEFAULT_USER, PasswordError, auth_path, remove_login, set_password
from rifferr.profile_store import app_db_path, config_root


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    p = sub.add_parser(
        "passwd",
        help="set the password for signing in from a browser; ends every session",
    )
    p.add_argument("--user", default=DEFAULT_USER, help=f"user name (default: {DEFAULT_USER})")
    p.add_argument(
        "--stdin", action="store_true", help="read the password once from standard input"
    )
    p.add_argument("--remove", action="store_true", help="remove the password login")
    p.set_defaults(func=cmd_passwd)
    s = sub.add_parser("sign-out", help="end browser sessions")
    s.add_argument("--all", action="store_true", required=True, help="end every session")
    s.set_defaults(func=cmd_sign_out)


def _end_sessions() -> int:
    db = AppDB(app_db_path())
    try:
        return db.sessions_delete_all()
    finally:
        db.close()


def _read_password(args: argparse.Namespace) -> str | None:
    if args.stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    if not sys.stdin.isatty():
        print("rifferr passwd: no terminal to ask on; pass --stdin", file=sys.stderr)
        return None
    first = getpass.getpass("New password: ")
    second = getpass.getpass("Again: ")
    if first != second:
        print("rifferr passwd: the passwords differ; nothing changed", file=sys.stderr)
        return None
    return first


def cmd_passwd(args: argparse.Namespace) -> int:
    path = auth_path(config_root())
    if args.remove:
        removed = remove_login(path)
        ended = _end_sessions()
        print("password login removed" if removed else "no password login was set")
        print(f"{ended} session(s) ended")
        return 0
    password = _read_password(args)
    if password is None:
        return 2
    try:
        login = set_password(path, password, args.user)
    except PasswordError as exc:
        print(f"rifferr passwd: {exc}", file=sys.stderr)
        return 2
    ended = _end_sessions()
    print(f"password set for user '{login.user}' in {path}")
    print(f"{ended} session(s) ended; sign in again")
    return 0


def cmd_sign_out(args: argparse.Namespace) -> int:
    ended = _end_sessions()
    print(f"{ended} session(s) ended")
    return 0
