"""S-TAGSCRIPT models: script files, actions, scopes (TAGSCRIPT-1, -2, -4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rifferr.core.workflow.loader import validate_model
from rifferr.core.workflow.yamldoc import parse
from rifferr.tagscript.models import (
    ACTION_KEYS,
    CopyAction,
    SetAction,
    TagScript,
    TagScriptParams,
    action_key,
)

REPO = Path(__file__).resolve().parents[3]


def errors(text: str) -> list[str]:
    doc = parse(text, "s.yaml")
    _, found = validate_model(TagScript, doc.plain(), doc, ())
    return [str(e) for e in found]


def test_TAGSCRIPT_1_actions_or_script_not_both() -> None:
    with pytest.raises(ValidationError, match="exactly one of 'actions'"):
        TagScriptParams.model_validate({"actions": [{"delete": ["x"]}], "script": "a.yaml"})
    with pytest.raises(ValidationError, match="exactly one of 'actions'"):
        TagScriptParams.model_validate({})
    assert TagScriptParams.model_validate({"script": "sub/ep.yaml"}).script == "sub/ep.yaml"


@pytest.mark.parametrize("path", ["/etc/x.yaml", "../x.yaml", "a/../../x.yaml", "~/x.yaml", ""])
def test_TAGSCRIPT_1_script_path_stays_in_scripts_folder(path: str) -> None:
    with pytest.raises(ValidationError):
        TagScriptParams.model_validate({"script": path})


def test_TAGSCRIPT_1_step_parameters_override_file() -> None:
    params = TagScriptParams.model_validate({"script": "x.yaml", "idempotence": "fail"})
    file = TagScript.model_validate({"scope": "album", "actions": [{"delete": ["x"]}]})
    merged = params.override(file)
    assert merged.idempotence == "fail"
    assert merged.scope == "album"
    inline = TagScriptParams.model_validate({"actions": [{"delete": ["x"]}], "scope": "item"})
    assert inline.inline() == TagScript(scope="item", actions=inline.actions or [])


def test_TAGSCRIPT_1_published_schema_matches_model() -> None:
    published = json.loads((REPO / "schemas" / "tagscript.schema.json").read_text())
    assert published == json.loads(json.dumps(TagScript.model_json_schema()))


def test_TAGSCRIPT_2_every_action_key_parses() -> None:
    raw: dict[str, Any] = {
        "set": {"album": "x"},
        "copy": {"from": "a", "to": "b"},
        "move": {"from": "a", "to": "b"},
        "replace": {"fields": ["a"], "find": "x", "with": "y"},
        "split": {"field": "a", "by": ";"},
        "join": {"field": "a", "with": ", "},
        "case": {"fields": ["a"], "style": "title"},
        "from_filename": {"pattern": "$track $title"},
        "delete": ["a"],
        "append_unique": {"field": "a", "values": ["x"]},
    }
    assert set(raw) == set(ACTION_KEYS)
    script = TagScript.model_validate({"actions": [{k: v} for k, v in raw.items()]})
    assert [action_key(a) for a in script.actions] == list(ACTION_KEYS)
    assert isinstance(script.actions[0], SetAction)
    assert isinstance(script.actions[1], CopyAction)


def test_TAGSCRIPT_2_errors_have_line_and_column() -> None:
    text = """\
actions:
  - set: {album: x}
    bogus: 1
  - {}
  - copy: {from: a, to: b}
    move: {from: a, to: b}
  - replace: {fields: [t], regex: '(', with: x}
  - just text
  - sett: {a: 1}
"""
    found = errors(text)
    assert found == [
        "s.yaml:3:5: actions[0].bogus: unknown key",
        "s.yaml:4:5: actions[1]: Value error, an action needs one of " + ", ".join(ACTION_KEYS),
        "s.yaml:5:5: actions[2]: Value error, an action takes exactly one action key, "
        "got copy, move",
        "s.yaml:7:14: actions[3].replace: Value error, invalid regex: "
        "missing ), unterminated subpattern at position 0",
        "s.yaml:8:5: actions[4]: an action must be a mapping with one action key",
        "s.yaml:9:5: actions[5]: Value error, an action needs one of "
        + ", ".join(ACTION_KEYS)
        + " (unknown key: sett)",
    ]


def test_TAGSCRIPT_2_field_names_and_specs_are_checked() -> None:
    assert errors("actions:\n  - copy: {from: a, to: B}\n")[0].startswith(
        "s.yaml:2:25: actions[0].copy.to:"
    )
    with pytest.raises(ValidationError, match="exactly one of 'find'"):
        TagScript.model_validate(
            {"actions": [{"replace": {"fields": ["a"], "find": "x", "regex": "y", "with": ""}}]}
        )
    with pytest.raises(ValidationError, match="placeholder"):
        TagScript.model_validate({"actions": [{"from_filename": {"pattern": "no fields"}}]})
    with pytest.raises(ValidationError, match="at least 1"):
        TagScript.model_validate({"actions": []})


def test_TAGSCRIPT_4_item_script_refuses_album_actions() -> None:
    with pytest.raises(ValidationError, match=r"actions\[1\]: album-scoped action"):
        TagScript.model_validate(
            {"scope": "item", "actions": [{"delete": ["a"]}, {"delete": ["b"], "scope": "album"}]}
        )
    with pytest.raises(ValidationError, match="album-scoped action"):
        TagScriptParams.model_validate(
            {"scope": "item", "actions": [{"delete": ["b"], "scope": "album"}]}
        )
    params = TagScriptParams.model_validate({"script": "x.yaml", "scope": "item"})
    with pytest.raises(ValueError, match="album-scoped action"):
        params.override(
            TagScript.model_validate({"actions": [{"delete": ["b"], "scope": "album"}]})
        )


def test_TAGSCRIPT_11_from_filename_is_item_only() -> None:
    with pytest.raises(ValidationError, match="item-scoped only"):
        TagScript.model_validate(
            {"actions": [{"from_filename": {"pattern": "$title"}, "scope": "album"}]}
        )
