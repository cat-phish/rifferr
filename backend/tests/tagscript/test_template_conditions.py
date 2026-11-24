"""C-TEMPLATE-CONDITIONS on FakeBackend: ``if`` / ``unless`` on tag-script actions and the
built-in template functions."""

from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.base import TemplateError
from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import is_true, run_script
from rifferr.tagscript.functions import startswith
from rifferr.tagscript.loader import parse_script
from rifferr.tagscript.models import TagScript, TagScriptParams
from rifferr.tagscript.preview import preview


def backend(**fields: Any) -> FakeBackend:
    return FakeBackend.from_fixture(
        {"albums": [{"album": "X", **fields, "items": [{"title": "t", "track": 1}]}]}
    )


def run(text: str, **fields: Any) -> dict[str, Any]:
    script, errors = parse_script(text, "t.yaml", str)
    assert script is not None, errors
    b = backend(**fields)
    (album,) = b.query(Selection())
    result = run_script(script, album, b)
    assert result.error is None, result.error
    return {d.field: d.after for d in result.diffs() if d.target.id == album.id}


def test_TEMPLATE_CONDITIONS_1_if_on_working_values() -> None:
    script = (
        "actions:\n"
        "  - set: { comments: 'A' }\n"
        "  - if: '%startswith{$comments,A}'\n"
        "    set: { label: yes }\n"
    )
    assert run(script) == {"comments": "A", "label": "yes"}


@pytest.mark.parametrize(
    ("result", "true"),
    [
        ("", False),
        ("  ", False),
        ("0", False),
        ("false", False),
        ("FALSE ", False),
        ("1", True),
        ("no", True),
        ("x", True),
    ],
)
def test_TEMPLATE_CONDITIONS_2_truth(result: str, true: bool) -> None:
    assert is_true(result) is true


def test_TEMPLATE_CONDITIONS_2_when_if_and_unless_all_hold() -> None:
    script = (
        "actions:\n"
        "  - when: 'album:X'\n"
        "    if: '$album'\n"
        "    unless: '%startswith{$label,L}'\n"
        "    set: { comments: ran }\n"
    )
    assert run(script, label="") == {"comments": "ran"}
    assert run(script, label="L") == {}
    assert run(script.replace("album:X", "album:Y"), label="") == {}


def test_TEMPLATE_CONDITIONS_3_template_error_fails_the_album() -> None:
    script, _ = parse_script("actions:\n  - if: '%nosuch{x}'\n    set: { a: b }\n", "t", str)
    assert script is not None
    b = backend()
    (album,) = b.query(Selection())
    result = run_script(script, album, b)
    assert result.error is not None
    assert "if '%nosuch{x}'" in str(result.error)


@pytest.mark.parametrize(
    ("text", "prefix", "out"),
    [("A / B - X", "A / B - ", "1"), ("X", "A", ""), ("a", "A", ""), ("X", "", "1")],
)
def test_TEMPLATE_CONDITIONS_4_startswith(text: str, prefix: str, out: str) -> None:
    assert startswith(text, prefix) == out


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("%startswith{$album,X}", "1"),
        ("%ifgreater{$year,2000,new,old}", "new"),
        ("%unpad{07}", "7"),
        ("%ifgreater{%unpad{$year},1999,a$,b,c}", "a,b"),
        ("$$x $% $}", "$x % }"),
    ],
)
def test_TEMPLATE_CONDITIONS_5_built_ins_on_fake_backend(template: str, expected: str) -> None:
    b = backend(year=2004)
    (album,) = b.query(Selection())
    assert b.evaluate(template, album) == expected


def test_TEMPLATE_CONDITIONS_5_other_functions_are_an_error() -> None:
    b = backend()
    (album,) = b.query(Selection())
    with pytest.raises(TemplateError, match="%the"):
        b.evaluate("%the{$album}", album)


def test_TEMPLATE_CONDITIONS_6_inline_actions_and_preview() -> None:
    params = TagScriptParams.model_validate(
        {"actions": [{"unless": "%startswith{$album,X}", "set": {"album": "X!"}}]}
    )
    script = params.inline()
    assert script is not None
    b = backend()
    (album,) = b.query(Selection())
    assert all(t.fields == [] for t in preview(script, [album], b).targets)


def test_TEMPLATE_CONDITIONS_7_schema_marks_templates() -> None:
    schema = TagScript.model_json_schema()
    text = str(schema)
    assert "'if'" in text
    assert "'unless'" in text


def test_TEMPLATE_CONDITIONS_6_choose_options_and_rules_files() -> None:
    from rifferr.choice_rules import RulesFile
    from rifferr.steps.tags_choose import TagsChooseParams

    rules = RulesFile.model_validate(
        {"rules": [{"label": "R", "when": "x:y", "actions": [{"if": "$a", "set": {"b": "c"}}]}]}
    )
    assert rules.rules[0].actions[0].if_ == "$a"
    params = TagsChooseParams.model_validate(
        {
            "query": "x:y",
            "options": [{"label": "O", "actions": [{"unless": "$a", "set": {"b": "c"}}]}],
        }
    )
    assert params.options[0].actions[0].unless == "$a"
