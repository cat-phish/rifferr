#!/usr/bin/env python3
"""Generate tiny audio fixtures with known tags for the test suite.

    python3 scripts/make_fixtures.py [--out backend/tests/fixtures/audio]

Each fixture is 0.5 s of a sine tone, encoded with ffmpeg in bit-exact mode so
re-running produces the same tags (DEV-TOOLING-8). Add fixtures by editing
FIXTURES; keep them tiny and keep their tags generic (no real releases).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "backend" / "tests" / "fixtures" / "audio"


@dataclass(frozen=True)
class Fixture:
    path: str  # relative to the output dir
    tags: dict[str, str] = field(default_factory=dict)
    frequency: int = 440


ALBUM = {
    "album": "Fixture Album",
    "album_artist": "Fixture Artist",
    "date": "2001",
    "genre": "Test",
}

FIXTURES: tuple[Fixture, ...] = (
    Fixture(
        "album-flac/01 First.flac",
        {**ALBUM, "artist": "Fixture Artist", "title": "First", "track": "1/2"},
        440,
    ),
    Fixture(
        "album-flac/02 Second.flac",
        {**ALBUM, "artist": "Fixture Artist", "title": "Second", "track": "2/2"},
        550,
    ),
    Fixture(
        "single-mp3/01 Lone.mp3",
        {
            "album": "Fixture Single",
            "album_artist": "Other Artist",
            "artist": "Other Artist",
            "title": "Lone",
            "track": "1/1",
            "date": "2010",
        },
        660,
    ),
)

CODEC = {
    ".flac": ["-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "4"],
}


def ffmpeg_cmd(ffmpeg: str, fx: Fixture, dest: Path) -> list[str]:
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={fx.frequency}:duration=0.5:sample_rate=22050",
        "-ac",
        "1",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:a",
        "+bitexact",
    ]
    for key, value in sorted(fx.tags.items()):
        cmd += ["-metadata", f"{key}={value}"]
    return [*cmd, *CODEC[dest.suffix], str(dest)]


def generate(out: Path, ffmpeg: str = "ffmpeg") -> list[Path]:
    if shutil.which(ffmpeg) is None:
        raise SystemExit(f"{ffmpeg} not found; install ffmpeg")
    written: list[Path] = []
    for fx in FIXTURES:
        dest = out / fx.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(ffmpeg_cmd(ffmpeg, fx, dest), check=True)
        written.append(dest)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--ffmpeg", default="ffmpeg")
    args = ap.parse_args(argv)
    for path in generate(args.out, args.ffmpeg):
        print(path.relative_to(args.out) if path.is_relative_to(args.out) else path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
