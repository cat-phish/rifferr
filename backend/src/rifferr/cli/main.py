"""Parser and dispatch for ``rifferr`` (CLI-14, CLI-15)."""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import sys

from rifferr import __version__
from rifferr.cli import files, profiles, run, serve


def version_text() -> str:
    """Rifferr and beets versions, without importing beets (only the beets backend may)."""
    try:
        beets = importlib.metadata.version("beets")
    except importlib.metadata.PackageNotFoundError:
        beets = "not installed"
    return f"rifferr {__version__}\nbeets {beets}"


class _Version(argparse.Action):
    """Like ``action="version"`` but keeps the line break between the two versions."""

    def __call__(self, parser: argparse.ArgumentParser, *args: object, **kw: object) -> None:
        print(version_text())
        parser.exit()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rifferr", description="Build and run music-import workflows with beets."
    )
    p.add_argument("--version", action=_Version, nargs=0, help="print versions and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="more log output")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    serve.add_parser(sub)
    run.add_parser(sub)
    files.add_parsers(sub)
    profiles.add_parser(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:  # one line per HTTP request is noise for `rifferr run`
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
