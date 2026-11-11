from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rifferr.core.workflow import (
    InterpolationContext,
    InterpolationError,
    find_expressions,
    interpolate,
)

CTX = InterpolationContext(
    vars={"inbox": "/srv/in", "n": "3"},
    run={"id": "run-1", "workflow": "two-step"},
    profile={"name": "main"},
    steps={"import": {"count": 2, "ok": True}},
)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{{ vars.inbox }}", "/srv/in"),
        ("{{vars.inbox}}", "/srv/in"),
        ("in={{  vars.inbox  }}/x", "in=/srv/in/x"),
        ("{{ run.id }}:{{ run.workflow }}", "run-1:two-step"),
        ("{{ profile.name }}", "main"),
        ("{{ steps.import.count }} albums", "2 albums"),
        ("{{ steps.import.ok }}", "true"),
        ("no expressions", "no expressions"),
    ],
)
def test_WORKFLOW_8_resolves_dotted_paths(template: str, expected: str) -> None:
    assert interpolate(template, CTX) == expected


@pytest.mark.parametrize(
    "template",
    [
        "{{ vars.missing }}",
        "{{ steps.other.count }}",
        "{{ steps.import.nope }}",
        "{{ secrets.x }}",
        "{{ vars }}",
    ],
)
def test_WORKFLOW_8_unresolvable_paths_raise(template: str) -> None:
    with pytest.raises(InterpolationError):
        interpolate(template, CTX)


@pytest.mark.parametrize(
    "template",
    [
        "{{ vars.x | upper }}",
        "{{ vars.x() }}",
        "{{ 1 + 2 }}",
        "{{ vars['x'] }}",
        "{{ }}",
        "{{ vars.inbox",
        "{{ __import__.os }}x{{",
    ],
)
def test_WORKFLOW_8_only_dotted_paths_no_code(template: str) -> None:
    with pytest.raises(InterpolationError, match="malformed"):
        interpolate(template, CTX)


def test_WORKFLOW_9_beets_template_syntax_is_untouched() -> None:
    template = "$albumartist - %if{$comp,Various,$album} [{{ vars.n }}] %upper{$title}}"
    assert (
        interpolate(template, CTX) == "$albumartist - %if{$comp,Various,$album} [3] %upper{$title}}"
    )


@given(st.text().filter(lambda s: "{{" not in s))
def test_WORKFLOW_9_strings_without_expressions_are_unchanged(text: str) -> None:
    assert interpolate(text, CTX) == text


def test_WORKFLOW_10_literal_braces_escape() -> None:
    assert interpolate("{{ '{{' }} vars.inbox }}", CTX) == "{{ vars.inbox }}"
    assert find_expressions("{{ '{{' }}") == []


def test_WORKFLOW_10_find_expressions_reports_offsets() -> None:
    found = find_expressions("a {{ vars.x }} b {{ run.id }}")
    assert [(e.path, e.start, e.end) for e in found] == [
        (("vars", "x"), 2, 14),
        (("run", "id"), 17, 29),
    ]
