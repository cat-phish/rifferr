"""S-TAGSCRIPT on real beets: working values, types, sharing, apply and re-run."""

from __future__ import annotations

from datetime import UTC, datetime

from rifferr.core.changeset import ChangeSet, ChangeSource, FieldChange, TargetChange
from rifferr.core.selection import Selection
from rifferr.tagscript.evaluator import check_idempotence, run_script
from rifferr.tagscript.models import TagScript
from tests.backends.beets.conftest import BeetsEnv, seed

SCRIPT = TagScript.model_validate(
    {
        "actions": [
            {
                "name": "ep-suffix",
                "when": "albumtype:ep ^album::[(]EP[)]$",
                "set": {"album": "$album (EP)", "original_year": "$year"},
            },
            {"name": "mark", "set": {"rifferr_note": "%upper{$albumtype}"}},
            {"replace": {"fields": ["title"], "find": "/", "with": "-"}, "scope": "item"},
            {"set": {"comments": "$album"}, "scope": "item"},
        ]
    }
)


def test_TAGSCRIPT_3_on_beets_working_values_types_and_inheritance(beets_env: BeetsEnv) -> None:
    backend = beets_env.backend()
    seed(
        backend,
        [
            {
                "albumartist": "Band",
                "album": "Live",
                "albumtype": "ep",
                "year": 2001,
                "items": [{"title": "A/B", "track": 1}, {"title": "C", "track": 2}],
            }
        ],
    )
    (album,) = backend.query(Selection())
    result = run_script(SCRIPT, album, backend)
    assert result.error is None, result.error
    by = {(d.target.id, d.field): d.after for d in result.diffs()}
    assert by[(album.id, "album")] == "Live (EP)"
    assert by[(album.id, "original_year")] == 2001  # coerced to the int field
    assert by[(album.id, "rifferr_note")] == "EP"  # beets template function
    first = album.items[0].id
    assert by[(first, "title")] == "A-B"
    assert by[(first, "comments")] == "Live (EP)"  # item saw the album's new value
    assert (first, "album") not in by  # carried by storing the album
    assert check_idempotence(SCRIPT, result, backend).ok

    # apply as the engine would, then a second run finds nothing to do
    targets: dict[str, list[FieldChange]] = {}
    refs = {}
    for d in result.diffs():
        targets.setdefault(d.target.id, []).append(
            FieldChange(field=d.field, old=d.before, new=d.after)
        )
        refs[d.target.id] = d.target
    cs = ChangeSet(
        id="cs-1",
        profile="test",
        source=ChangeSource(kind="step", name="tags.script"),
        created_at=datetime(2026, 11, 12, tzinfo=UTC),
        targets=[TargetChange(target=refs[t], changes=c) for t, c in targets.items()],
    )
    assert backend.check(cs) == []
    assert backend.apply(cs).applied
    (after,) = backend.query(Selection())
    assert after.items[0].fields["album"] == "Live (EP)"
    assert run_script(SCRIPT, after, backend).diffs() == []
