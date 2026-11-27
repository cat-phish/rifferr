"""Safety report entries for steps that send data to an AI provider (AI-CORE-20)."""

from __future__ import annotations

import textwrap

from rifferr.core.workflow import load_workflow, safety_report


def ai_findings(src: str) -> list[tuple[str, str, str]]:
    wf, _ = load_workflow(textwrap.dedent(src), "wf.yaml")
    return [
        (f.step_id, f.kind, f.detail)
        for f in safety_report(wf).findings
        if f.kind in ("ai_request", "path_sent")
    ]


def test_AI_CORE_20_ai_block_is_listed_with_what_it_sends() -> None:
    got = ai_findings(
        """
        name: t
        steps:
          - id: pick
            uses: shell
            with:
              run: "true"
              ai: {context: [albumartist, album, path], provider: local}
        """
    )
    assert got[0][:2] == ("pick", "ai_request")
    assert "albumartist, album, path" in got[0][2]
    assert "provider 'local'" in got[0][2]
    assert "leaves this machine" in got[0][2]
    assert got[1] == ("pick", "path_sent", "sends the file path field 'path' to the AI provider")


def test_AI_CORE_20_default_provider_and_no_ai() -> None:
    got = ai_findings(
        """
        name: t
        steps:
          - id: pick
            uses: shell
            with: {run: "true", ai: {}}
          - id: plain
            uses: shell
            with: {run: "true"}
        """
    )
    assert got == [
        (
            "pick",
            "ai_request",
            "sends album data (no album fields) to the profile's default provider; it leaves "
            "this machine unless the provider's host is local",
        )
    ]
