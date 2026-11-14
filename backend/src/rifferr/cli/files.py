"""``rifferr validate`` and ``rifferr trust`` (CLI-11, CLI-12). Neither needs a server."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from rifferr.appdb import AppDB
from rifferr.core.profile import Profile, ProfileError, ProfileLoadError
from rifferr.core.workflow import (
    Calls,
    SafetyReport,
    ValidationError,
    Workflow,
    WorkflowLoadError,
    load_workflow,
    safety_report,
    validate,
)
from rifferr.engine import StepTypeRegistry
from rifferr.profile_store import (
    WORKFLOWS_DIR,
    app_db_path,
    config_root,
    load_profile,
    profile_dir,
)
from rifferr.steps.registry import load_step_types
from rifferr.trust import WorkflowFileError, read_workflow_file
from rifferr.workflow_calls import called_trust, read_calls

EXIT_OK, EXIT_INVALID, EXIT_USAGE = 0, 1, 2


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    v = sub.add_parser("validate", help="check workflow files")
    v.add_argument("files", nargs="+", type=Path)
    v.add_argument("--profile", help="also check against this profile's variables")
    v.add_argument("--safety", action="store_true", help="print the static safety report")
    v.set_defaults(func=cmd_validate)
    t = sub.add_parser("trust", help="trust a workflow file's exact content")
    t.add_argument("file", type=Path)
    t.add_argument("--profile", help="validate against this profile first")
    t.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    t.set_defaults(func=cmd_trust)


@dataclass
class Checked:
    path: Path
    text: str
    sha256: str
    workflow: Workflow | None
    errors: list[ValidationError]
    calls: Calls | None = None
    folder: Path | None = None
    """Where called files are read from: the profile's workflows/ folder, else the file's."""

    def where(self, e: ValidationError) -> Path:
        """The file an error is in: the workflow, or a file it calls."""
        if self.folder is not None and self.calls is not None and e.source in self.calls.files:
            return self.folder / e.source
        return self.path


def check_file(path: Path, registry: StepTypeRegistry, profile: Profile | None) -> Checked:
    """Read once, validate those bytes (no backend capabilities: never loads beets).
    Called workflows are read from the profile's workflows/ folder, or without a
    profile from the file's own folder (S-SUBWORKFLOW)."""
    try:
        text, sha = read_workflow_file(path)
    except (OSError, WorkflowFileError) as exc:
        message = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        return Checked(path, "", "", None, [ValidationError("", "", message)])
    try:
        wf, doc = load_workflow(text, str(path))
    except WorkflowLoadError as exc:
        return Checked(path, text, sha, None, list(exc.errors))
    profile_vars = dict(profile.vars) if profile is not None else None
    folder = (
        profile_dir(config_root(), profile.name) / WORKFLOWS_DIR
        if profile is not None
        else path.parent
    )
    calls = read_calls(folder, wf, doc, path)
    errors = validate(wf, registry, None, profile_vars, doc=doc, calls=calls)
    return Checked(path, text, sha, wf, errors, calls, folder)


def format_error(path: Path, e: ValidationError) -> str:
    pos = ":".join(str(x) for x in (e.line, e.column) if x is not None)
    where = f"{path}:{pos}" if pos else str(path)
    return f"{where}: {e.path}: {e.message}" if e.path else f"{where}: {e.message}"


def print_safety(path: Path, report: SafetyReport) -> None:
    if not report.findings:
        print(f"{path}: safety: no findings")
    for f in report.findings:
        origin = f" ({f.workflow})" if f.workflow else ""
        print(f"{path}: safety: [{f.step_id}]{origin} {f.kind}: {f.detail}")


def _registry() -> StepTypeRegistry:
    registry = load_step_types()
    for problem in registry.errors:
        print(f"warning: {problem}", file=sys.stderr)
    return registry


def _profile(name: str | None, prog: str) -> Profile | int | None:
    if name is None:
        return None
    try:
        return load_profile(config_root(), name)
    except (ProfileError, ProfileLoadError) as exc:
        print(f"rifferr {prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE


def cmd_validate(args: argparse.Namespace) -> int:
    profile = _profile(args.profile, "validate")
    if isinstance(profile, int):
        return profile
    if profile is None:
        print("note: no --profile given; profile variable checks skipped", file=sys.stderr)
    registry = _registry()
    status = EXIT_OK
    for path in args.files:
        c = check_file(path, registry, profile)
        for e in c.errors:
            print(format_error(c.where(e), e))
        if c.errors:
            status = EXIT_INVALID
        else:
            print(f"{path}: ok")
        if args.safety and c.workflow is not None:
            print_safety(path, safety_report(c.workflow, c.calls))
    return status


def cmd_trust(args: argparse.Namespace) -> int:
    profile = _profile(args.profile, "trust")
    if isinstance(profile, int):
        return profile
    path: Path = args.file
    c = check_file(path, _registry(), profile)
    if c.workflow is None or (profile is not None and c.errors):
        for e in c.errors:
            print(format_error(c.where(e), e), file=sys.stderr)
        print("rifferr trust: not trusting an invalid workflow", file=sys.stderr)
        return EXIT_INVALID
    db = AppDB(app_db_path())
    try:
        return _trust(args, c, db)
    finally:
        db.close()


def _trust(args: argparse.Namespace, c: Checked, db: AppDB) -> int:
    """Trust the file and every untrusted file it calls, each by its own hash
    (SUBWORKFLOW-13)."""
    path = c.path
    assert c.workflow is not None
    untrusted = [f for f in called_trust(db, c.calls) if not f.trusted]
    print(f"{path}: sha256 {c.sha256}")
    folder = c.folder or path.parent
    for f in untrusted:
        print(f"{folder / f.path}: called, not trusted: sha256 {f.sha256}")
    print_safety(path, safety_report(c.workflow, c.calls))
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "rifferr trust: no terminal to confirm on; pass --yes to trust without asking",
                file=sys.stderr,
            )
            return EXIT_USAGE
        what = (
            "this content" if not untrusted else f"this content and {len(untrusted)} called file(s)"
        )
        answer = input(f"Trust exactly {what}? Steps like shell may then run it. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not trusted")
            return EXIT_INVALID
    db.trust(c.sha256, str(path.resolve()), "cli")  # the bytes shown above (CLI-12)
    print(f"trusted {path}")
    for f in untrusted:
        db.trust(f.sha256, str((folder / f.path).resolve()), "cli")
        print(f"trusted {folder / f.path}")
    return EXIT_OK
