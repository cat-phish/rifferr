from __future__ import annotations

import pytest

from rifferr.core.workflow import load_workflow

SOURCE = """\
# Imports new music, then lists it.
name: two-step   # shown in the UI
vars:
  inbox: { description: "Folder new downloads land in" }

steps:
  # first: import
  - id: import
    uses: beets.import
    with: { path: "{{ vars.inbox }}", mode: quiet }
  - id: list
    uses: shell
    apply: auto
    with:
      run: ['ls', "-la", $path]   # quoting styles differ on purpose
      timeout: 30
"""


def test_WORKFLOW_11_load_and_dump_is_byte_identical() -> None:
    _, doc = load_workflow(SOURCE, "wf.yaml")
    assert doc.dump() == SOURCE


def test_WORKFLOW_11_edit_preserves_comments_order_and_quoting() -> None:
    _, doc = load_workflow(SOURCE, "wf.yaml")
    doc.set(("steps", 1, "with", "timeout"), 60)
    out = doc.dump()
    # ruamel.yaml keeps comments, order, quoting and block layout. The only
    # normalization is whitespace inside flow mappings: "{ a: b }" -> "{a: b}".
    expected = (
        SOURCE.replace("timeout: 30", "timeout: 60")
        .replace(
            '{ description: "Folder new downloads land in" }',
            '{description: "Folder new downloads land in"}',
        )
        .replace(
            '{ path: "{{ vars.inbox }}", mode: quiet }', '{path: "{{ vars.inbox }}", mode: quiet}'
        )
    )
    assert out == expected


def test_WORKFLOW_11_reload_after_edit_reflects_change() -> None:
    _, doc = load_workflow(SOURCE, "wf.yaml")
    doc.set(("name",), "renamed")
    wf, _ = load_workflow(doc.dump(), "wf.yaml")
    assert wf.name == "renamed"
    assert "# shown in the UI" in doc.dump()


def test_WORKFLOW_11_set_rejects_missing_parent() -> None:
    _, doc = load_workflow(SOURCE, "wf.yaml")
    with pytest.raises(KeyError):
        doc.set(("nope", "x"), 1)
