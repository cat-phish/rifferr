"""S-TAGSCRIPT evaluator: actions, working values, net diff, idempotence check."""

from __future__ import annotations

from typing import Any

import pytest

from rifferr.backends.fake import FakeBackend
from rifferr.core.changeset import TargetRef
from rifferr.core.snapshot import AlbumSnapshot
from rifferr.tagscript.evaluator import (
    FieldDiff,
    ScriptResult,
    change_case,
    check_idempotence,
    match_filename,
    run_script,
)
from rifferr.tagscript.models import TagScript

SLASH = "\u2215"  # DIVISION SLASH, a filename-safe stand-in for "/"


def library(**options: Any) -> FakeBackend:
    return FakeBackend.from_fixture(
        {
            "albums": [
                {
                    "albumartist": "Band",
                    "album": "Live Sessions",
                    "albumtype": "ep",
                    "year": 2001,
                    "genres": ["Rock"],
                    "path": "/music/Band/Live Sessions",
                    "items": [
                        {
                            "title": "A/B",
                            "track": 2,
                            "path": f"/music/Band/Live Sessions/02 - A{SLASH}B.flac",
                        },
                        {
                            "title": "intro",
                            "track": 1,
                            "path": "/music/Band/Live Sessions/01 - Intro.flac",
                        },
                    ],
                }
            ]
        },
        **options,
    )


def only_album(fb: FakeBackend) -> AlbumSnapshot:
    from rifferr.core.selection import Selection

    (album,) = fb.query(Selection())
    return album


def script(*actions: dict[str, Any], **top: Any) -> TagScript:
    return TagScript.model_validate({**top, "actions": list(actions)})


def run(fb: FakeBackend, *actions: dict[str, Any], **top: Any) -> ScriptResult:
    return run_script(script(*actions, **top), only_album(fb), fb)


def diffs(result: ScriptResult) -> dict[tuple[str, str], Any]:
    return {(d.target.id, d.field): (None if d.deleted else d.after) for d in result.diffs()}


def item(result: ScriptResult, index: int) -> str:
    return result.album.items[index].id


# -- order, working values, sharing --------------------------------------------


def test_TAGSCRIPT_3_actions_see_earlier_results() -> None:
    fb = library()
    r = run(
        fb,
        {"set": {"album": "$album (EP)"}},
        {"set": {"albumdisambig": "[$album]"}, "when": "album::EP"},
    )
    assert r.error is None
    a = r.album.id
    assert diffs(r)[(a, "album")] == "Live Sessions (EP)"
    assert diffs(r)[(a, "albumdisambig")] == "[Live Sessions (EP)]"


def test_TAGSCRIPT_3_set_fields_in_order_each_sees_previous() -> None:
    r = run(library(), {"set": {"album": "X", "albumdisambig": "$album!"}})
    assert diffs(r)[(r.album.id, "albumdisambig")] == "X!"


def test_TAGSCRIPT_3_album_change_of_shared_field_reaches_items() -> None:
    fb = library()
    r = run(fb, {"set": {"album": "New"}}, {"set": {"comments": "$album"}, "scope": "item"})
    assert r.error is None
    first = item(r, 0)
    assert r.working[TargetRef(scope="item", id=first)]["album"] == "New"
    assert diffs(r)[(first, "comments")] == "New"
    # the item's own album change follows from the album's and isn't proposed twice
    assert (first, "album") not in diffs(r)


def test_TAGSCRIPT_3_unshared_album_field_stays_on_album() -> None:
    fb = library(inherited_fields=frozenset({"album"}))
    r = run(fb, {"set": {"label": "Indie"}}, {"set": {"comments": "[$label]"}, "scope": "item"})
    first = item(r, 0)
    assert "label" not in r.working[TargetRef(scope="item", id=first)]
    # FakeBackend item templates still see the stored album's fields; here none
    assert diffs(r)[(first, "comments")] == "[$label]"


def test_TAGSCRIPT_4_item_actions_run_per_item_in_track_order() -> None:
    fb = library()
    r = run(
        fb, {"append_unique": {"field": "comments_list", "values": ["$track"]}, "scope": "item"}
    )
    order = [d.target.id for d in r.diffs()]
    assert order == [r.album.items[0].id, r.album.items[1].id]
    assert r.album.items[0].fields["track"] == 1


def test_TAGSCRIPT_5_when_skips_non_matching_targets() -> None:
    r = run(library(), {"set": {"title": "x"}, "scope": "item", "when": "track:2"})
    assert diffs(r) == {(item(r, 1), "title"): "x"}


def test_TAGSCRIPT_5_when_uses_working_values() -> None:
    r = run(
        library(),
        {"set": {"albumtype": "album"}},
        {"set": {"album": "$album (EP)"}, "when": "albumtype:ep"},
    )
    assert (r.album.id, "album") not in diffs(r)


# -- actions -------------------------------------------------------------------


def test_TAGSCRIPT_6_set_templates_and_coerces() -> None:
    r = run(library(), {"set": {"year": "$year", "original_year": "1999", "comp": True}})
    d = diffs(r)
    assert (r.album.id, "year") not in d  # unchanged after coercion
    assert d[(r.album.id, "original_year")] == 1999
    assert d[(r.album.id, "comp")] is True


def test_TAGSCRIPT_6_set_list_templates_each_element() -> None:
    r = run(library(), {"set": {"genres": ["$albumtype", "Live"]}})
    assert diffs(r)[(r.album.id, "genres")] == ["ep", "Live"]


def test_TAGSCRIPT_7_copy_keeps_value_and_type() -> None:
    r = run(library(), {"copy": {"from": "year", "to": "original_year"}})
    assert diffs(r)[(r.album.id, "original_year")] == 2001


def test_TAGSCRIPT_7_copy_coerces_when_types_differ() -> None:
    r = run(library(), {"copy": {"from": "year", "to": "albumdisambig"}})
    assert diffs(r)[(r.album.id, "albumdisambig")] == "2001"


def test_TAGSCRIPT_7_move_deletes_source_and_absent_from_does_nothing() -> None:
    r = run(library(), {"move": {"from": "albumtype", "to": "releasetype"}})
    d = diffs(r)
    assert d[(r.album.id, "releasetype")] == "ep"
    assert d[(r.album.id, "albumtype")] is None
    r = run(library(), {"move": {"from": "nothing", "to": "albumtype"}})
    assert diffs(r) == {}


def test_TAGSCRIPT_8_replace_literal_and_regex_on_lists() -> None:
    r = run(
        library(),
        {"replace": {"fields": ["title"], "find": "/", "with": SLASH}, "scope": "item"},
        {
            "replace": {
                "fields": ["genres", "year"],
                "regex": "^(r)ock$",
                "with": r"\1oll",
                "flags": ["i"],
            }
        },
    )
    d = diffs(r)
    assert d[(item(r, 1), "title")] == f"A{SLASH}B"
    assert d[(r.album.id, "genres")] == ["Roll"]
    assert (r.album.id, "year") not in d  # non-string values are left alone


def test_TAGSCRIPT_9_split_and_join() -> None:
    r = run(
        library(),
        {"set": {"artists_text": "A ; B;; C"}},
        {"split": {"field": "artists_text", "by": ";", "into": "artists"}},
        {"join": {"field": "genres", "with": " / ", "into": "genre_text"}},
    )
    d = diffs(r)
    assert d[(r.album.id, "artists")] == ["A", "B", "C"]
    assert d[(r.album.id, "genre_text")] == "Rock"


def test_TAGSCRIPT_9_split_on_a_list_flattens() -> None:
    r = run(library(), {"set": {"genres": ["a/b", "c"]}}, {"split": {"field": "genres", "by": "/"}})
    assert diffs(r)[(r.album.id, "genres")] == ["a", "b", "c"]


def test_TAGSCRIPT_9_join_leaves_strings() -> None:
    r = run(library(), {"join": {"field": "album", "with": ", "}})
    assert diffs(r) == {}


@pytest.mark.parametrize(
    ("text", "style", "keep", "expected"),
    [
        ("the BEST of", "title", [], "The BEST Of"),
        ("the best of the ep", "title", ["of", "the", "EP"], "the Best of the EP"),
        ("hello WORLD. again", "sentence", [], "Hello WORLD. again"),
        ("(live) at home", "title", [], "(Live) At Home"),
        ("Mixed CASE", "lower", ["CASE"], "mixed CASE"),
        ("abc", "upper", [], "ABC"),
        ("  spaced  out ", "title", [], "  Spaced  Out "),
    ],
)
def test_TAGSCRIPT_10_case_styles(text: str, style: Any, keep: list[str], expected: str) -> None:
    assert change_case(text, style, keep) == expected


def test_TAGSCRIPT_10_case_action_on_items() -> None:
    r = run(library(), {"case": {"fields": ["title"], "style": "title"}, "scope": "item"})
    assert diffs(r) == {(item(r, 0), "title"): "Intro"}


def test_TAGSCRIPT_11_from_filename_sets_fields_through_coerce() -> None:
    r = run(library(), {"from_filename": {"pattern": "$track - $title"}})
    d = diffs(r)
    assert d[(item(r, 0), "title")] == "Intro"
    assert d[(item(r, 1), "title")] == f"A{SLASH}B"
    assert (item(r, 0), "track") not in d  # "01" -> 1, unchanged
    assert r.notes == []


def test_TAGSCRIPT_11_from_filename_no_match_is_a_note() -> None:
    r = run(library(), {"from_filename": {"pattern": "$disc-$track $title", "source": "name"}})
    assert diffs(r) == {}
    assert len(r.notes) == 2
    assert "doesn't match" in r.notes[0].message


def test_TAGSCRIPT_11_match_filename() -> None:
    assert match_filename("$track - $title", "01 - A - B") == {"track": "01", "title": "A - B"}
    assert match_filename("[${catalognum}] $album", "[X-1] Name") == {
        "catalognum": "X-1",
        "album": "Name",
    }
    assert match_filename("$track - $title", "no dash") is None


def test_TAGSCRIPT_12_delete_and_absent() -> None:
    r = run(library(), {"delete": ["albumtype", "nothing"]})
    assert diffs(r) == {(r.album.id, "albumtype"): None}
    (d,) = r.diffs()
    assert d.deleted


def test_TAGSCRIPT_12_deleted_field_is_absent_for_later_actions() -> None:
    r = run(library(), {"delete": ["albumtype"]}, {"set": {"album": "X"}, "when": "albumtype:ep"})
    assert (r.album.id, "album") not in diffs(r)


def test_TAGSCRIPT_13_append_unique_keeps_order_and_skips_present() -> None:
    r = run(
        library(),
        {"append_unique": {"field": "genres", "values": ["Live", "Rock", "Live", "$albumtype"]}},
    )
    assert diffs(r)[(r.album.id, "genres")] == ["Rock", "Live", "ep"]


# -- result ----------------------------------------------------------------------


def test_TAGSCRIPT_14_net_difference_only() -> None:
    r = run(library(), {"set": {"album": "Temp"}}, {"set": {"album": "Live Sessions"}})
    assert r.diffs() == []


def test_TAGSCRIPT_14_rationale_names_actions() -> None:
    r = run(
        library(),
        {"name": "ep-suffix", "set": {"album": "$album (EP)"}},
        {"replace": {"fields": ["album"], "find": "Live", "with": "Studio"}},
    )
    (d,) = r.diffs()
    assert isinstance(d, FieldDiff)
    assert d.before == "Live Sessions"
    assert r.rationale(d) == "ep-suffix, action 2"


def test_TAGSCRIPT_15_errors_name_action_field_and_target() -> None:
    r = run(library(), {"set": {"album": "ok"}}, {"name": "bad", "set": {"year": "soon"}})
    assert r.error is not None
    assert r.error.action == "bad"
    assert r.error.action_index == 1
    assert r.error.field == "year"
    assert r.error.target.id == r.album.id
    assert "bad (#2)" in str(r.error)
    assert "expects a whole number" in str(r.error)


def test_TAGSCRIPT_15_template_and_query_errors() -> None:
    r = run(library(), {"set": {"album": "%upper{$album}"}})
    assert r.error is not None
    assert "template" in r.error.message
    r = run(library(), {"set": {"album": "x"}, "when": "bare-term"})
    assert r.error is not None
    assert r.error.field is None
    assert "when" in r.error.message


def test_TAGSCRIPT_16_idempotent_script_passes() -> None:
    fb = library()
    s = script(
        {
            "name": "ep-suffix",
            "when": r"albumtype:ep ^album::[(]EP[)]$",
            "set": {"album": "$album (EP)"},
        }
    )
    first = run_script(s, only_album(fb), fb)
    check = check_idempotence(s, first, fb)
    assert check.ok


def test_TAGSCRIPT_16_second_pass_change_names_the_action() -> None:
    fb = library()
    s = script({"name": "ep-suffix", "when": "albumtype:ep", "set": {"album": "$album (EP)"}})
    first = run_script(s, only_album(fb), fb)
    check = check_idempotence(s, first, fb)
    assert not check.ok
    (issue,) = check.issues
    assert issue.action == "ep-suffix"
    assert issue.field == "album"
    assert issue.first == "Live Sessions (EP)"
    assert issue.second == "Live Sessions (EP) (EP)"


def test_TAGSCRIPT_16_second_pass_respects_deletes() -> None:
    fb = library()
    s = script({"delete": ["albumtype"]}, {"set": {"album": "$album!"}, "when": "albumtype:ep"})
    first = run_script(s, only_album(fb), fb)
    assert check_idempotence(s, first, fb).ok
