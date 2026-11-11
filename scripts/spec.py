#!/usr/bin/env python3
"""Spec lookup and lint for Rifferr's spec-driven workflow.

Stdlib only. Lets humans and agents read one spec section or one behavior
instead of whole files, and checks specs follow specs/_template.md.

    spec.py list [--status S] [--milestone M]
    spec.py show <SPEC-ID> [--section NAME]
    spec.py behavior <BEHAVIOR-ID>
    spec.py tests [<SPEC-ID>]
    spec.py lint
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPECS = ROOT / "specs"
TESTS = [ROOT / "backend" / "tests", ROOT / "frontend" / "src", ROOT / "frontend" / "e2e"]

STATUSES = ("Draft", "Approved", "Implemented", "Verified")
MILESTONES = tuple(f"M{i}" for i in range(6))
SECTIONS = (
    "Context",
    "Behavior",
    "Interfaces",
    "Examples",
    "Extensibility",
    "Security & safety",
    "Non-goals",
    "Open questions",
    "Tasks",
)
CATEGORY_DIRS = {"core": "C", "steps": "S", "features": "F"}
SKIP = {"_template.md"}
OVERVIEW = "000-overview.md"

TITLE_RE = re.compile(r"^# (?P<id>[A-Z0-9][A-Z0-9-]*): (?P<title>.+)$")
FIELD_RE = re.compile(r"^(?P<key>Status|Milestone): *(?P<value>.*)$")
SECTION_RE = re.compile(r"^## (?P<name>.+?)\s*$")
BEHAVIOR_RE = re.compile(r"^\s*(?:[-*]|\d+\.)?\s*\**(?P<id>[A-Z][A-Z0-9-]*-\d+)\**:")


@dataclass
class Spec:
    path: Path
    id: str = ""
    title: str = ""
    status: str = ""
    milestone: str = ""
    sections: dict[str, list[str]] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    behaviors: dict[str, str] = field(default_factory=dict)
    behavior_lines: list[tuple[int, str]] = field(default_factory=list)

    @property
    def rel(self) -> str:
        return str(self.path.relative_to(ROOT))

    @property
    def behavior_prefix(self) -> str:
        # S-TAGSCRIPT -> TAGSCRIPT; behaviors are TAGSCRIPT-1, TAGSCRIPT-2, ...
        return re.sub(r"^[CSF]-", "", self.id)


def parse(path: Path) -> Spec:
    spec = Spec(path=path)
    current: str | None = None
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not spec.id and (m := TITLE_RE.match(line)):
            spec.id, spec.title = m["id"], m["title"].strip()
            continue
        if current is None and (m := FIELD_RE.match(line)):
            setattr(spec, m["key"].lower(), m["value"].strip())
            continue
        if m := SECTION_RE.match(line):
            current = m["name"]
            spec.section_order.append(current)
            spec.sections[current] = []
            continue
        if current is not None:
            spec.sections[current].append(line)
            if current == "Behavior" and (m := BEHAVIOR_RE.match(line)):
                spec.behavior_lines.append((lineno, m["id"]))
                spec.behaviors.setdefault(m["id"], line.strip())
    return spec


def load_all() -> list[Spec]:
    paths = sorted(p for p in SPECS.rglob("*.md") if p.name not in SKIP)
    return [parse(p) for p in paths]


def find(specs: list[Spec], spec_id: str) -> Spec:
    for s in specs:
        if s.id.upper() == spec_id.upper():
            return s
    sys.exit(f"spec not found: {spec_id}")


def cmd_list(args: argparse.Namespace) -> int:
    for s in load_all():
        if args.status and s.status != args.status:
            continue
        if args.milestone and s.milestone != args.milestone:
            continue
        print(f"{s.id:<20} {s.status:<12} {s.milestone:<4} {s.rel}  {s.title}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    s = find(load_all(), args.id)
    if not args.section:
        print(s.path.read_text(encoding="utf-8"), end="")
        return 0
    want = args.section.lower()
    for name, lines in s.sections.items():
        if name.lower().startswith(want):
            print(f"# {s.id} — {name}  ({s.rel})")
            print("\n".join(lines).strip())
            return 0
    sys.exit(f"{s.id}: no section matching {args.section!r}; have {s.section_order}")


def cmd_behavior(args: argparse.Namespace) -> int:
    want = args.id.upper().replace("_", "-")
    for s in load_all():
        if want in s.behaviors:
            print(f"{s.rel}: {s.behaviors[want]}")
            return 0
    sys.exit(f"behavior not found: {args.id}")


def test_text() -> str:
    chunks: list[str] = []
    for base in TESTS:
        if base.exists():
            for p in base.rglob("*"):
                if p.suffix in {".py", ".ts", ".tsx"} and p.is_file():
                    chunks.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def cmd_tests(args: argparse.Namespace) -> int:
    specs = load_all()
    if args.id:
        specs = [find(specs, args.id)]
    corpus = test_text()
    missing = 0
    for s in specs:
        for bid in s.behaviors:
            token = bid.replace("-", "_")
            ok = re.search(rf"\b\w*{re.escape(token)}(?!\d)", corpus) is not None
            if not ok:
                missing += 1
            print(f"{'ok     ' if ok else 'MISSING'} {bid:<24} {s.rel}")
    return 1 if missing else 0


def lint_spec(s: Spec, errors: list[str]) -> None:
    def err(msg: str) -> None:
        errors.append(f"{s.rel}: {msg}")

    if not s.id:
        err("first heading must be '# <ID>: Title'")
        return
    if s.status not in STATUSES:
        err(f"Status {s.status!r} not in {STATUSES}")
    if s.milestone not in MILESTONES:
        err(f"Milestone {s.milestone!r} not in M0–M5")
    if s.path.name == OVERVIEW:
        return

    category = s.path.parent.name
    expected_prefix = CATEGORY_DIRS.get(category)
    if expected_prefix is None or s.path.parent.parent != SPECS:
        err(f"spec must live in specs/{{{','.join(CATEGORY_DIRS)}}}/")
    elif not s.id.startswith(f"{expected_prefix}-"):
        err(f"ID {s.id} must start with '{expected_prefix}-' in specs/{category}/")
    if s.path.stem != s.id:
        err(f"filename {s.path.name} must be {s.id}.md")

    order = [n for n in s.section_order if n in SECTIONS]
    missing = [n for n in SECTIONS if n not in s.section_order]
    if missing:
        err(f"missing sections: {missing}")
    if order != [n for n in SECTIONS if n in order]:
        err(f"sections out of order: {order}")
    extra = [n for n in s.section_order if n not in SECTIONS]
    if extra:
        err(f"unknown sections: {extra} (allowed: {list(SECTIONS)})")

    prefix = s.behavior_prefix
    for lineno, bid in s.behavior_lines:
        if not re.fullmatch(rf"{re.escape(prefix)}-\d+", bid):
            err(f"line {lineno}: behavior {bid} must be {prefix}-<n>")
    if s.status in ("Approved", "Implemented", "Verified") and not s.behavior_lines:
        err(f"Status {s.status} requires at least one behavior")
    if s.status in ("Implemented", "Verified"):
        open_tasks = [ln for ln in s.sections.get("Tasks", []) if ln.lstrip().startswith("- [ ]")]
        if open_tasks:
            err(f"Status {s.status} but {len(open_tasks)} unticked task(s)")


def cmd_lint(_: argparse.Namespace) -> int:
    errors: list[str] = []
    specs = load_all()
    seen_ids: dict[str, str] = {}
    seen_prefixes: dict[str, str] = {}
    seen_behaviors: dict[str, str] = {}
    for s in specs:
        lint_spec(s, errors)
        if not s.id:
            continue
        if s.id in seen_ids:
            errors.append(f"{s.rel}: duplicate spec ID {s.id} (also {seen_ids[s.id]})")
        seen_ids[s.id] = s.rel
        if s.path.name != OVERVIEW:
            p = s.behavior_prefix
            if p in seen_prefixes:
                errors.append(f"{s.rel}: behavior prefix {p} collides with {seen_prefixes[p]}")
            seen_prefixes[p] = s.rel
        for _, bid in s.behavior_lines:
            if bid in seen_behaviors and seen_behaviors[bid] != s.rel:
                errors.append(f"{s.rel}: behavior {bid} also defined in {seen_behaviors[bid]}")
            elif bid in seen_behaviors:
                errors.append(f"{s.rel}: behavior {bid} defined twice")
            seen_behaviors[bid] = s.rel
    for e in errors:
        print(e, file=sys.stderr)
    if not errors:
        print(f"ok: {len(specs)} spec(s)")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="spec.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="one line per spec")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--milestone", choices=MILESTONES)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="print a spec, or one section of it")
    p.add_argument("id")
    p.add_argument("--section", "-s", help="section name or prefix, e.g. Behavior")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("behavior", help="print one behavior line by ID")
    p.add_argument("id", help="e.g. TAGSCRIPT-4 or TAGSCRIPT_4")
    p.set_defaults(func=cmd_behavior)

    p = sub.add_parser("tests", help="report behaviors with no test naming them")
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_tests)

    p = sub.add_parser("lint", help="check all specs against the template")
    p.set_defaults(func=cmd_lint)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
