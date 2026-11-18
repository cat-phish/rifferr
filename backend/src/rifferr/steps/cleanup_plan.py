"""Listing and cleaning a folder for ``files.cleanup`` (S-FILES-CLEANUP).

``plan`` walks a folder and classifies every file and folder (FILES-CLEANUP-3 to -8,
-17); ``execute`` deletes or trashes what the plan marked, re-checking each file first
(FILES-CLEANUP-9, -10, -15); ``purge_trash`` empties old run folders (FILES-CLEANUP-16).
Library knowledge comes in as functions, so the backend stays behind its protocol.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Final

from rifferr.core.files import BusyFolder, CleanupEntry, EntryKind

TRASH_MARKER: Final = ".rifferr-trash"
RUN_ID_PATTERN: Final = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
LIBRARY_CHUNK: Final = 500


@dataclass
class Plan:
    root: Path
    entries: list[CleanupEntry] = field(default_factory=list[CleanupEntry])
    skipped: list[str] = field(default_factory=list[str])
    """Paths not walked, with why (outside the folder, unreadable)."""

    def totals(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.entries:
            out[e.cls] = out.get(e.cls, 0) + 1
        return out


class CleanupError(ValueError):
    """The folder (or trash) can't be cleaned at all; the step fails without a target."""


# -- patterns -----------------------------------------------------------------------


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Shell-style glob over relative ``/`` paths: ``*`` and ``?`` stay inside one path
    part, ``**`` spans parts, ``[...]`` is a set. A pattern without ``/`` matches the
    file name at any depth (as ``.gitignore`` does)."""
    if "/" not in pattern.strip("/"):
        pattern = "**/" + pattern.strip("/")
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif c == "*":
            out, i = out + "[^/]*", i + 1
        elif c == "?":
            out, i = out + "[^/]", i + 1
        elif c == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out, i = out + re.escape(c), i + 1
            else:
                body = pattern[i + 1 : end].replace("\\", "\\\\")
                if body.startswith("!"):
                    body = "^" + body[1:]
                out, i = out + f"[{body}]", end + 1
        else:
            out, i = out + re.escape(c), i + 1
    try:
        return re.compile(f"^{out}$", re.IGNORECASE)
    except re.error as exc:
        raise CleanupError(f"bad pattern {pattern!r}: {exc}") from None


class Patterns:
    def __init__(self, patterns: Iterable[str]) -> None:
        self.patterns = list(patterns)
        self._compiled = [_glob_regex(p) for p in self.patterns]

    def match(self, rel: str) -> str | None:
        """The first pattern ``rel`` matches, or None."""
        for pattern, rx in zip(self.patterns, self._compiled, strict=True):
            if rx.match(rel):
                return pattern
        return None


# -- guards (FILES-CLEANUP-3) -------------------------------------------------------


def check_root(root: Path, library_dirs: Sequence[Path], home: Path | None) -> Path:
    """The absolute, real folder to clean, or ``CleanupError``."""
    if not root.is_absolute():
        raise CleanupError(f"path must be absolute: {root}")
    if not root.is_dir() or root.is_symlink():
        raise CleanupError(f"path is not a folder: {root}")
    real = Path(os.path.realpath(root))
    if real == Path(real.anchor):
        raise CleanupError("refusing to clean the file system root")
    if home is not None:
        home_real = Path(os.path.realpath(home))
        if real == home_real or home_real.is_relative_to(real):  # SEC-05
            raise CleanupError("refusing to clean the home folder or a folder holding it")
    for lib in library_dirs:
        lib_real = Path(os.path.realpath(lib))
        if lib_real == real or lib_real.is_relative_to(real):
            raise CleanupError(f"refusing to clean {real}: it holds the library folder {lib}")
        if real.is_relative_to(lib_real):  # SEC-05
            raise CleanupError(f"refusing to clean {real}: it is inside the library folder {lib}")
    return real


def check_trash(trash: Path, root: Path) -> Path:
    """FILES-CLEANUP-15: absolute, outside ``root``, and either new, empty or marked."""
    if not trash.is_absolute():
        raise CleanupError(f"trash must be absolute: {trash}")
    real = Path(os.path.realpath(trash))
    if real == root or real.is_relative_to(root) or root.is_relative_to(real):
        raise CleanupError("trash must not be inside the cleaned folder, or contain it")
    if real.exists():
        if not real.is_dir():
            raise CleanupError(f"trash is not a folder: {trash}")
        if any(real.iterdir()) and not (real / TRASH_MARKER).is_file():
            raise CleanupError(
                f"trash {trash} is not empty and wasn't made by Rifferr "
                f"(no {TRASH_MARKER}); refusing to use it"
            )
    return real


# -- listing (FILES-CLEANUP-4 to -8, -17) --------------------------------------------


def plan(
    root: Path,
    *,
    delete: Sequence[str],
    keep: Sequence[str],
    empty_dirs: bool,
    remove_root: bool,
    busy: Sequence[BusyFolder],
    library_files: Callable[[list[Path]], set[Path]],
    importable: Callable[[list[Path]], set[Path]],
    allow_audio: bool,
) -> Plan:
    out = Plan(root)
    deletes, keeps = Patterns(delete), Patterns(keep)
    files: list[tuple[str, Path, os.stat_result, EntryKind]] = []
    dirs: list[tuple[str, Path]] = []  # walk order: parents before children
    busy_real = [(Path(os.path.realpath(b.path)), b.reason) for b in busy]

    def busy_reason(path: Path) -> str | None:
        for folder, reason in busy_real:
            if path == folder or path.is_relative_to(folder):
                return reason
        return None

    def holds_busy(path: Path) -> bool:
        return any(folder.is_relative_to(path) for folder, _ in busy_real)

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=None):
        here = Path(current)
        rel_here = _rel(root, here)
        dirs.append((rel_here, here))
        keep_dirs: list[str] = []
        for name in sorted(dirnames):
            child = here / name
            if child.is_symlink():  # listed as a link, never walked into (FILES-CLEANUP-4)
                files.append((_rel(root, child), child, child.lstat(), "link"))
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in sorted(filenames):
            child = here / name
            try:
                st = child.lstat()
            except OSError as exc:
                out.skipped.append(f"{_rel(root, child)}: {exc.strerror}")
                continue
            kind: EntryKind = "link" if stat.S_ISLNK(st.st_mode) else "file"
            files.append((_rel(root, child), child, st, kind))

    regular = [p for _, p, _, kind in files if kind == "file"]
    known: set[Path] = set()
    for start in range(0, len(regular), LIBRARY_CHUNK):
        known |= library_files(regular[start : start + LIBRARY_CHUNK])
    music = set() if allow_audio else importable(regular)

    going: set[str] = set()
    for rel, path, st, kind in files:
        size = st.st_size if kind == "file" else None
        common = {"path": rel, "kind": kind, "size": size, "mtime_ns": st.st_mtime_ns}
        reason = busy_reason(path)
        if reason is not None:
            out.entries.append(CleanupEntry(**common, cls="busy", reason=reason))
            continue
        if path in known:
            out.entries.append(CleanupEntry(**common, cls="in_library"))
            continue
        if keeps.match(rel) is not None:
            out.entries.append(CleanupEntry(**common, cls="keep", reason=keeps.match(rel)))
            continue
        matched = deletes.match(rel)
        if matched is not None and path in music:
            out.entries.append(CleanupEntry(**common, cls="left", reason="importable"))
            continue
        if matched is not None:
            out.entries.append(CleanupEntry(**common, cls="delete", reason=matched))
            going.add(rel)
            continue
        out.entries.append(CleanupEntry(**common, cls="left"))

    if empty_dirs:
        out.entries += _empty_dirs(root, dirs, files, going, busy_reason, holds_busy, remove_root)
    return out


def _empty_dirs(
    root: Path,
    dirs: list[tuple[str, Path]],
    files: list[tuple[str, Path, os.stat_result, EntryKind]],
    going: set[str],
    busy_reason: Callable[[Path], str | None],
    holds_busy: Callable[[Path], bool],
    remove_root: bool,
) -> list[CleanupEntry]:
    """Folders with nothing left in them once ``going`` is gone, deepest first."""
    staying: dict[str, int] = {rel: 0 for rel, _ in dirs}
    for rel, _, _, _ in files:
        if rel not in going:
            staying[_parent(rel)] = staying.get(_parent(rel), 0) + 1
    empty: list[CleanupEntry] = []
    for rel, path in sorted(dirs, key=lambda d: d[0].count("/") + (d[0] != "."), reverse=True):
        if staying.get(rel, 0) or busy_reason(path) is not None or holds_busy(path):
            if rel != ".":
                staying[_parent(rel)] = staying.get(_parent(rel), 0) + 1
            continue
        if rel == "." and not remove_root:
            continue
        st = path.lstat()
        empty.append(CleanupEntry(path=rel, kind="dir", cls="empty", mtime_ns=st.st_mtime_ns))
    return empty


def _rel(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel or "."


def _parent(rel: str) -> str:
    parent = PurePosixPath(rel).parent.as_posix()
    return "." if parent in ("", ".") else parent


# -- cleaning (FILES-CLEANUP-9, -10, -15) ---------------------------------------------


@dataclass
class Outcome:
    deleted: int = 0
    moved: int = 0
    dirs_removed: int = 0
    failed: int = 0
    changed: int = 0
    bytes_freed: int = 0
    lines: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    """(stream, line) for the run log."""


def execute(
    p: Plan,
    *,
    trash: Path | None,
    run_id: str,
    only: set[str] | None = None,
) -> Outcome:
    """Delete or trash the plan's ``delete`` files, then remove its ``empty`` folders.
    ``only``: relative paths a person reviewed (FILES-CLEANUP-14); others are left."""
    result = Outcome()
    if trash is not None:
        trash.mkdir(parents=True, exist_ok=True)
        (trash / TRASH_MARKER).touch(exist_ok=True)
    for entry in p.entries:
        if entry.cls != "delete" or (only is not None and entry.path not in only):
            continue
        path = p.root / entry.path
        try:
            dfd, name = _open_parent(p.root, entry.path)
        except FileNotFoundError:
            result.lines.append(("info", f"{entry.path}: already gone"))
            continue
        except OSError:  # a parent is now a link or not a folder (SEC-04)
            result.changed += 1
            result.lines.append(("error", f"{entry.path}: changed since it was listed; left"))
            continue
        try:
            try:
                st = os.lstat(name, dir_fd=dfd)
            except FileNotFoundError:
                result.lines.append(("info", f"{entry.path}: already gone"))
                continue
            size_changed = entry.kind == "file" and st.st_size != entry.size
            if size_changed or st.st_mtime_ns != entry.mtime_ns:
                result.changed += 1
                result.lines.append(("error", f"{entry.path}: changed since it was listed; left"))
                continue
            try:
                if trash is not None:
                    target = _free(trash / run_id / entry.path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        tfd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                        try:
                            os.rename(name, target.name, src_dir_fd=dfd, dst_dir_fd=tfd)
                        finally:
                            os.close(tfd)
                    except OSError as exc:
                        if exc.errno != errno.EXDEV:
                            raise
                        # other file system: the parent folder must still be the one we hold
                        if not os.path.samestat(os.fstat(dfd), os.stat(path.parent)):
                            raise
                        shutil.move(path, target)
                    result.moved += 1
                    result.lines.append(("info", f"{entry.path}: moved to trash"))
                else:
                    os.unlink(name, dir_fd=dfd)
                    result.deleted += 1
                    result.lines.append(("info", f"{entry.path}: deleted"))
                result.bytes_freed += entry.size or 0
            except OSError as exc:
                result.failed += 1
                result.lines.append(("error", f"{entry.path}: can't remove: {exc.strerror}"))
        finally:
            os.close(dfd)
    for entry in p.entries:  # deepest first, as listed
        if entry.cls != "empty" or (only is not None and entry.path not in only):
            continue
        try:
            if entry.path == ".":
                p.root.rmdir()
            else:
                dfd, name = _open_parent(p.root, entry.path)
                try:
                    os.rmdir(name, dir_fd=dfd)
                finally:
                    os.close(dfd)
            result.dirs_removed += 1
            result.lines.append(("info", f"{entry.path}/: removed empty folder"))
        except FileNotFoundError:
            continue
        except OSError as exc:  # not empty any more, or no permission: leave it
            result.lines.append(("info", f"{entry.path}/: left ({exc.strerror})"))
    return result


def _open_parent(root: Path, rel: str) -> tuple[int, str]:
    """SEC-04: a descriptor for the folder holding ``rel``, reached one part at a time
    without following links, and the entry's name in it. Caller closes the descriptor."""
    *dirs, name = Path(rel).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in dirs:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, name


def _free(target: Path) -> Path:
    if not target.exists() and not target.is_symlink():
        return target
    n = 1
    while True:
        candidate = target.with_name(f"{target.stem}.{n}{target.suffix}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def _same_fs(a: Path, b_dir: Path) -> bool:
    try:
        return a.lstat().st_dev == b_dir.stat().st_dev
    except OSError:
        return False


# -- trash_keep (FILES-CLEANUP-16) ------------------------------------------------------


def purge_trash(trash: Path, keep: timedelta, now: datetime) -> tuple[list[str], list[str]]:
    """Delete run folders in a marked trash whose newest entry is older than ``keep``.
    Returns (removed run ids, other entries left alone)."""
    if not (trash / TRASH_MARKER).is_file():
        return [], []
    removed: list[str] = []
    others: list[str] = []
    cutoff = (now - keep).timestamp()
    for child in sorted(trash.iterdir()):
        if child.name == TRASH_MARKER:
            continue
        if not RUN_ID_PATTERN.match(child.name) or child.is_symlink() or not child.is_dir():
            others.append(child.name)
            continue
        if _newest(child) < cutoff:
            shutil.rmtree(child)
            removed.append(child.name)
    return removed, others


def _newest(folder: Path) -> float:
    newest = folder.lstat().st_mtime
    for current, dirnames, filenames in os.walk(folder, followlinks=False):
        for name in [*dirnames, *filenames]:
            try:
                newest = max(newest, (Path(current) / name).lstat().st_mtime)
            except OSError:
                continue
    return newest


__all__ = [
    "TRASH_MARKER",
    "BusyFolder",
    "CleanupEntry",
    "CleanupError",
    "Outcome",
    "Patterns",
    "Plan",
    "check_root",
    "check_trash",
    "execute",
    "plan",
    "purge_trash",
]
