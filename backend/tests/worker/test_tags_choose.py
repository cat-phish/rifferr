"""The ``tags.choose`` step in the worker, and choice reviews decided through the
supervisor (S-TAGS-CHOOSE TAGS-CHOOSE-1 to -11, -13)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from rifferr.appdb import AppChoiceReviews, ChoiceResolvedError
from rifferr.backends.fake import FakeBackend
from rifferr.choice_rules import load_rules
from rifferr.core.choices import ChoiceReview
from rifferr.core.facts import ArtistNames
from rifferr.core.selection import Selection
from rifferr.engine import StepTypeRegistry
from rifferr.engine.choices import ChoiceDecisionError
from rifferr.profile_store import config_root, load_profile
from rifferr.steps.review import ReviewStep
from rifferr.steps.tags_choose import TagsChooseParams, TagsChooseStep
from rifferr.worker.process import Worker
from tests.engine.steps import ALL
from tests.worker.conftest import wf
from tests.worker.test_gates import RUN, Harness, serving

AFTER = "{id: after, uses: test.echo, with: {message: 'after choose'}}"

KIND = """{id: kind, uses: tags.choose, with: {
  show: [albumartist],
  options: [
    {label: Collaboration, suggest: {when: 'rf_share_multi_artist_tracks:51..'},
     actions: [{set: {albumkind: collab, albumartist: Collaboration}}]},
    {label: Split, suggest: {when: 'rf_count_albumartists:2..4'},
     actions: [{set: {albumkind: split, albumartist: Split}}]},
    {label: Compilation, actions: [{set: {albumkind: compilation, albumartist: Compilation}}]}
  ],
  custom: [albumartist, albumartists]%s}}"""


def kind(extra: str = "") -> str:
    return wf(KIND % extra, AFTER)


class ChooseHarness(Harness):
    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path, FakeBackend())
        self.backend.names = ArtistNames(joiners=(" & ", "/"))
        self.worker = Worker(
            load_profile(config_root(), "main"),
            self.db,
            StepTypeRegistry([*ALL, ReviewStep(), TagsChooseStep()]),
            self.backend,
            self._send,
        )
        self.reviews = AppChoiceReviews(self.db)
        self.rules = config_root() / "profiles" / "main" / "rules"

    def album_with(self, name: str, items: list[dict[str, Any]], **fields: Any) -> None:
        from rifferr.backends.base import RUN_MARKER

        self.backend.add_album({"album": name, RUN_MARKER: RUN, **fields, "items": items})

    def fields(self, name: str) -> dict[str, Any]:
        (album,) = [a for a in self.backend.query(Selection()) if a.fields["album"] == name]
        return dict(album.fields)

    def review(self) -> ChoiceReview:
        (review,) = self.reviews.pending(run_id=RUN)
        return review


@pytest.fixture
async def h(tmp_path: Path) -> AsyncIterator[ChooseHarness]:
    harness = ChooseHarness(tmp_path)
    async with serving(harness):
        yield harness
    harness.db.close()


def split_album(h: ChooseHarness, name: str = "S") -> None:
    h.album_with(
        name,
        [{"title": "a", "artist": "A"}, {"title": "b", "artist": "B"}],
        albumartist="A & B",
        albumartists=["A", "B"],
    )


async def waiting(h: ChooseHarness, source: str) -> ChoiceReview:
    h.submit(source)
    await h.until(lambda: h.state() == "waiting_review")
    return h.review()


# -- TAGS-CHOOSE-1 .. -4: options, suggestion, review --------------------------------


async def test_TAGS_CHOOSE_1_options_that_change_something_plus_custom(h: ChooseHarness) -> None:
    split_album(h)
    h.backend.add_album({"album": "C", "albumkind": "compilation", "albumartist": "Compilation"})
    review = await waiting(h, kind())
    assert [o.label for o in review.options] == ["Collaboration", "Split", "Compilation"]
    assert [c.field for c in review.custom] == ["albumartist", "albumartists"]
    preview = {(p.field, p.after) for p in review.option("o1").preview}  # type: ignore[union-attr]
    assert preview == {("albumkind", "split"), ("albumartist", "Split")}


async def test_TAGS_CHOOSE_1_an_option_that_changes_nothing_is_left_out(
    h: ChooseHarness,
) -> None:
    h.album_with(
        "S",
        [{"title": "a", "artist": "A"}, {"title": "b", "artist": "B"}],
        albumartist="Compilation",
        albumartists=["A", "B"],
        albumkind="compilation",
    )
    review = await waiting(h, kind())
    assert "Compilation" not in [o.label for o in review.options]


async def test_TAGS_CHOOSE_2_first_matching_suggestion_with_album_facts(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    assert review.suggested == "o1"  # 2 album artists; the tracks have one artist each


async def test_TAGS_CHOOSE_2_collaboration_when_most_tracks_have_several_artists(
    h: ChooseHarness,
) -> None:
    h.album_with(
        "K",
        [
            {"title": "a", "artist": "A & B", "artists": ["A", "B"]},
            {"title": "b", "artist": "A & B", "artists": ["A", "B"]},
        ],
        albumartist="A & B",
        albumartists=["A", "B"],
    )
    review = await waiting(h, kind())
    assert review.suggested == "o0"


async def test_TAGS_CHOOSE_2_no_suggestion_when_none_holds(h: ChooseHarness) -> None:
    h.album_with("M", [{"title": "a", "artist": "A"}], albumartist="A")
    review = await waiting(h, kind())
    assert review.suggested is None


async def test_TAGS_CHOOSE_3_only_the_album_waits(h: ChooseHarness) -> None:
    split_album(h)
    h.submit(wf(KIND % ", query: 'rf_joined_albumartist:1'", AFTER))
    h.album_with("Solo", [{"title": "x", "artist": "Solo"}], albumartist="Solo")
    await h.until(lambda: h.state() == "waiting_review")
    review = h.review()
    assert review.label.endswith("S")
    assert h.logs().count("after choose") == 1  # Solo went on
    (requested,) = h.events("choice.requested")
    assert requested.data["review_id"] == review.id


async def test_TAGS_CHOOSE_4_review_shows_fields_and_custom_inputs(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    assert review.shown == {"albumartist": "A & B"}
    kinds = {c.field: (c.kind, c.value) for c in review.custom}
    assert kinds == {"albumartist": ("text", "A & B"), "albumartists": ("list", ["A", "B"])}


# -- TAGS-CHOOSE-5, -6: decisions ----------------------------------------------------


async def test_TAGS_CHOOSE_5_choosing_an_option_applies_one_changeset(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    job = await h.supervisor.decide_choice(review.id, "choose", option="o2", by="ui")
    assert job is not None
    await h.until(lambda: h.state() == "succeeded")
    fields = h.fields("S")
    assert (fields["albumkind"], fields["albumartist"]) == ("compilation", "Compilation")
    (applied,) = h.changesets.for_run(RUN, "applied")
    assert applied.changeset.rationale == "Compilation (chosen in review)"
    assert applied.changeset.source.name == "tags.choose"
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert (stored.state, stored.chosen, stored.changeset_id) == (
        "chosen",
        "o2",
        applied.changeset.id,
    )
    assert h.logs()[-1] == "after choose"
    with pytest.raises(ChoiceResolvedError):
        await h.supervisor.decide_choice(review.id, "skip")


async def test_TAGS_CHOOSE_5_custom_values_are_set_literally(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    await h.supervisor.decide_choice(
        review.id,
        "choose",
        option="custom",
        values={"albumartist": "Split $x", "albumartists": ["A", "B", "C"]},
        note="three bands",
    )
    await h.until(lambda: h.state() == "succeeded")
    fields = h.fields("S")
    assert (fields["albumartist"], fields["albumartists"]) == ("Split $x", ["A", "B", "C"])
    (applied,) = h.changesets.for_run(RUN, "applied")
    assert applied.changeset.rationale == "custom (chosen in review): three bands"


async def test_TAGS_CHOOSE_5_a_decision_that_does_not_fit_fails_at_once(
    h: ChooseHarness,
) -> None:
    split_album(h)
    review = await waiting(h, kind())
    with pytest.raises(ChoiceDecisionError):
        await h.supervisor.decide_choice(review.id, "choose", option="nope")
    with pytest.raises(ChoiceDecisionError):
        await h.supervisor.decide_choice(
            review.id, "choose", option="custom", values={"title": "x"}
        )
    assert h.review().state == "pending"


async def test_TAGS_CHOOSE_5_undoing_the_run_restores_the_album(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    await h.supervisor.decide_choice(review.id, "choose", option="o1")
    await h.until(lambda: h.state() == "succeeded")
    assert h.fields("S")["albumartist"] == "Split"
    await h.supervisor.undo(run_id=RUN)
    await h.until(lambda: h.fields("S")["albumartist"] == "A & B")
    assert "albumkind" not in h.fields("S")


async def test_TAGS_CHOOSE_6_skip_leaves_the_album_and_continues(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    assert await h.supervisor.decide_choice(review.id, "skip", by="ui") is None
    await h.until(lambda: h.state() == "succeeded")
    assert h.fields("S")["albumartist"] == "A & B"
    assert h.logs()[-1] == "after choose"


async def test_TAGS_CHOOSE_6_reject_stops_the_lane(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    await h.supervisor.decide_choice(review.id, "reject", by="ui")
    await h.until(lambda: h.state() in ("succeeded", "failed", "cancelled"))
    assert "after choose" not in h.logs()
    finished = [e.data for e in h.events("lane.finished")]
    assert any(d.get("state") == "rejected" for d in finished)


async def test_TAGS_CHOOSE_6_reject_with_continue_goes_on(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind(", on_reject: continue"))
    await h.supervisor.decide_choice(review.id, "reject", by="ui")
    await h.until(lambda: h.state() == "succeeded")
    assert h.logs()[-1] == "after choose"


async def test_TAGS_CHOOSE_3_cancelling_the_run_cancels_its_reviews(h: ChooseHarness) -> None:
    split_album(h)
    review = await waiting(h, kind())
    await h.supervisor.cancel(RUN)
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert stored.state == "cancelled"


# -- TAGS-CHOOSE-7: when_single ------------------------------------------------------

SINGLE = """{id: kind, uses: tags.choose, with: {when_single: auto, options: [
  {label: Mark, actions: [{set: {albumkind: split}}]},
  {label: Same, actions: [{set: {album: S}}]}]}}"""


async def test_TAGS_CHOOSE_7_one_changing_option_applies_without_review(
    h: ChooseHarness,
) -> None:
    split_album(h)
    h.submit(wf(SINGLE, AFTER))
    await h.until(lambda: h.state() == "succeeded")
    assert h.fields("S")["albumkind"] == "split"
    assert h.reviews.pending(run_id=RUN) == []
    finished = h.events("run.step.finished")[0].data
    (choice,) = finished["outputs"]["choices"]
    assert (choice["chosen"], choice["decided_by"]) == ("Mark", "auto")


# -- TAGS-CHOOSE-8 .. -10: rules files -----------------------------------------------

RULES = """# genre buckets
target: genres
rules:
  - label: Rock
    when: 'subgenres::(?i)rock|punk'
    actions: [{ set: { genres: [Rock] } }]
  - label: Electronic
    when: 'subgenres::(?i)house|techno'
    actions: [{ set: { genres: [Electronic] } }]
fallback:
  template: "{{ value }}"
  actions: [{ set: { genres: ["{{ choice }}"] } }]
"""

BUCKET = (
    "{id: bucket, uses: tags.choose, with: {options_from: genres.yaml, rank_by: subgenres, "
    "custom: [genres]%s}}"
)


def bucket(extra: str = "") -> str:
    return wf(BUCKET % extra, AFTER)


def genre_album(h: ChooseHarness, *subgenres: str) -> None:
    h.backend.field_types["subgenres"] = list
    h.album_with("G", [{"title": "a"}], subgenres=list(subgenres), genres=[])
    h.rules.mkdir(exist_ok=True)
    (h.rules / "genres.yaml").write_text(RULES)


async def test_TAGS_CHOOSE_8_rules_ranked_by_matching_values(h: ChooseHarness) -> None:
    genre_album(h, "Deep House", "Techno", "Post-Punk")
    review = await waiting(h, bucket())
    assert [(o.label, o.matched) for o in review.options] == [("Electronic", 2), ("Rock", 1)]
    assert review.suggested == review.options[0].key


async def test_TAGS_CHOOSE_9_fallback_offers_values_no_rule_matches(h: ChooseHarness) -> None:
    genre_album(h, "Shoegaze", "Punk")
    review = await waiting(h, bucket())
    assert [o.label for o in review.options] == ["Rock", "Shoegaze"]
    (fallback,) = [o for o in review.options if o.label == "Shoegaze"]
    assert fallback.actions == [{"set": {"genres": ["Shoegaze"]}}]


async def test_TAGS_CHOOSE_10_learn_appends_a_rule_and_keeps_comments(h: ChooseHarness) -> None:
    genre_album(h, "Shoegaze", "Dream Pop", "Punk")
    review = await waiting(h, bucket(", learn: true"))
    await h.supervisor.decide_choice(
        review.id, "choose", option="custom", values={"genres": ["Dream Pop"]}
    )
    await h.until(lambda: h.state() == "succeeded")
    text = (h.rules / "genres.yaml").read_text()
    assert text.startswith("# genre buckets")
    assert "label: Dream Pop" in text
    assert "'subgenres::^Shoegaze$|^Dream Pop$'" in text
    learned = load_rules(h.rules / "genres.yaml").rules[-1]
    (album,) = h.backend.query(Selection())
    dream = album.model_copy(update={"fields": {**album.fields, "subgenres": ["Dream Pop"]}})
    assert h.backend.matches(learned.when, dream)
    stored = h.reviews.get(review.id)
    assert stored is not None
    assert stored.learned is not None
    assert "Dream Pop" in stored.learned
    assert h.fields("G")["genres"] == ["Dream Pop"]


async def test_TAGS_CHOOSE_8_a_rules_file_outside_rules_fails(h: ChooseHarness) -> None:
    genre_album(h, "Punk")
    h.submit(wf(BUCKET.replace("genres.yaml", "../profile.yaml") % "", AFTER))
    await h.until(lambda: h.state() == "failed")


# -- TAGS-CHOOSE-11: outputs ---------------------------------------------------------


async def test_TAGS_CHOOSE_11_outputs(h: ChooseHarness) -> None:
    split_album(h)
    await waiting(h, kind())
    finished = h.events("run.step.finished")[0].data
    outputs = finished["outputs"]
    (choice,) = outputs["choices"]
    assert choice["offered"] == ["Collaboration", "Split", "Compilation"]
    assert choice["suggested"] == "Split"
    assert choice["chosen"] is None
    assert outputs["asked"] == 1


# -- TAGS-CHOOSE-13: known names -----------------------------------------------------


async def test_TAGS_CHOOSE_13_this_is_one_artist_remembers_the_name(h: ChooseHarness) -> None:
    h.album_with("D", [{"title": "a", "artist": "AC/DC"}], albumartist="AC/DC")
    review = await waiting(h, kind(", known_name: albumartist, known_name_option: Compilation"))
    assert (review.known_name_value, review.known_name_option) == ("AC/DC", "Compilation")
    await h.supervisor.decide_choice(review.id, "choose", known_name=True)
    await h.until(lambda: h.state() == "succeeded")
    assert "AC/DC" in h.backend.names.names
    assert h.fields("D")["albumkind"] == "compilation"
    assert not h.backend.names.has_joiner("AC/DC")


async def test_TAGS_CHOOSE_13_known_name_without_an_option_changes_nothing(
    h: ChooseHarness,
) -> None:
    h.album_with("D", [{"title": "a", "artist": "AC/DC"}], albumartist="AC/DC")
    review = await waiting(h, kind(", known_name: albumartist, known_name_option: 'Single artist'"))
    await h.supervisor.decide_choice(review.id, "choose", known_name=True)
    await h.until(lambda: h.state() == "succeeded")
    assert "AC/DC" in h.backend.names.names
    assert h.fields("D")["albumartist"] == "AC/DC"
    assert h.changesets.for_run(RUN, "applied") == []


# -- parameters ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "problem"),
    [
        ({}, "give options"),
        ({"custom": ["a"], "rank_by": "genres"}, "rank_by ranks"),
        ({"custom": ["a"], "learn": True}, "learn needs"),
        ({"custom": ["a"], "known_name_option": "X"}, "needs known_name"),
        ({"options_from": "../x.yaml", "custom": ["a"]}, "rules/ folder"),
        (
            {
                "options": [
                    {"label": "A", "actions": [{"set": {"a": "1"}}]},
                    {"label": "A", "actions": [{"set": {"a": "2"}}]},
                ]
            },
            "unique",
        ),
    ],
)
def test_TAGS_CHOOSE_1_parameter_validation(params: dict[str, Any], problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        TagsChooseParams.model_validate(params)
