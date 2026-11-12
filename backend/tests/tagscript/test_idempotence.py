"""TAGSCRIPT-19: idempotence properties of the built-in actions (Hypothesis)."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from rifferr.backends.fake import FakeBackend
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import check_idempotence, run_script
from rifferr.tagscript.models import TagScript

FIELDS = ["album", "comments", "genres", "label"]
TYPES: dict[str, type] = {"genres": list, "track": int}

# Text without template syntax, so literal values stay literal.
text = st.text(
    st.characters(blacklist_characters="$%{}", blacklist_categories=("Cs",)), max_size=12
)
words = st.lists(text, max_size=4)
field = st.sampled_from(FIELDS)
value = st.one_of(text, words)


def backend(album: dict[str, Any], titles: list[str]) -> FakeBackend:
    items = [
        {"title": t, "track": n + 1, "path": f"/m/{n + 1:02d} - {t or 'x'}.flac"}
        for n, t in enumerate(titles)
    ]
    return FakeBackend.from_fixture({"albums": [{**album, "items": items}]}, field_types=TYPES)


albums = st.fixed_dictionaries({}, optional={f: value for f in FIELDS})
titles = st.lists(text, min_size=1, max_size=3)

copy_spec = st.builds(lambda a, b: {"from": a, "to": b}, field, field)
actions = st.one_of(
    copy_spec.map(lambda s: {"copy": s}),
    copy_spec.map(lambda s: {"move": s}),
    st.builds(
        lambda f, by, into, strip: {"split": {"field": f, "by": by, "into": into, "strip": strip}},
        field,
        st.text(min_size=1, max_size=2),
        st.none() | field,
        st.booleans(),
    ),
    st.builds(
        lambda f, w, into: {"join": {"field": f, "with": w, "into": into}},
        field,
        text,
        st.none() | field,
    ),
    st.builds(
        lambda fs, style, keep: {"case": {"fields": fs, "style": style, "keep": keep}},
        st.lists(field, min_size=1, max_size=2),
        st.sampled_from(["lower", "upper", "title", "sentence"]),
        st.lists(text.filter(bool), max_size=2),
    ),
    st.builds(lambda fs: {"delete": fs}, st.lists(field, min_size=1, max_size=2)),
    st.builds(
        lambda f, vs: {"append_unique": {"field": f, "values": vs}},
        field,
        st.lists(text, min_size=1, max_size=3),
    ),
)
item_actions = st.one_of(
    st.just({"from_filename": {"pattern": "$track - $title"}}),
    st.just({"from_filename": {"pattern": "$track - $comments", "source": "stem"}}),
    st.builds(lambda a: {**a, "scope": "item"}, actions),
)


def _check(album: dict[str, Any], names: list[str], action: dict[str, Any]) -> None:
    fb = backend(album, names)
    (snapshot,) = fb.query(Selection())
    s = TagScript.model_validate({"actions": [action]})
    first = run_script(s, snapshot, fb)
    if first.error is not None:
        return  # e.g. "01" into an int field is fine, but text into 'track' is an error
    check = check_idempotence(s, first, fb)
    assert check.ok, (action, check.issues, check.error)


SETTINGS = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(albums, titles, actions)
def test_TAGSCRIPT_19_single_actions_are_idempotent(
    album: dict[str, Any], names: list[str], action: dict[str, Any]
) -> None:
    _check(album, names, action)


@SETTINGS
@given(albums, titles, item_actions)
def test_TAGSCRIPT_19_item_actions_are_idempotent(
    album: dict[str, Any], names: list[str], action: dict[str, Any]
) -> None:
    _check(album, names, action)


@SETTINGS
@given(
    albums,
    titles,
    st.dictionaries(
        field, st.one_of(text, st.just("$album"), st.just("$album $label")), min_size=1
    ),
)
def test_TAGSCRIPT_19_set_check_reports_exactly_second_pass_changes(
    album: dict[str, Any], names: list[str], assignments: dict[str, str]
) -> None:
    fb = backend(album, names)
    (snapshot,) = fb.query(Selection())
    s = TagScript.model_validate({"actions": [{"set": assignments}]})
    first = run_script(s, snapshot, fb)
    if first.error is not None:
        return
    second = run_script(s, snapshot, fb, start=first.working)
    changed = second.working != first.working
    assert check_idempotence(s, first, fb).ok == (not changed)


@SETTINGS
@given(albums, titles, st.lists(field, min_size=1, max_size=2), text, text)
def test_TAGSCRIPT_19_replace_check_reports_exactly_second_pass_changes(
    album: dict[str, Any], names: list[str], fields: list[str], find: str, with_: str
) -> None:
    if not find:
        return
    fb = backend(album, names)
    (snapshot,) = fb.query(Selection())
    s = TagScript.model_validate(
        {"actions": [{"replace": {"fields": fields, "find": find, "with": with_}}]}
    )
    first = run_script(s, snapshot, fb)
    second = run_script(s, snapshot, fb, start=first.working)
    changed = second.working != first.working
    assert check_idempotence(s, first, fb).ok == (not changed)
