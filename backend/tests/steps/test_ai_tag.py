"""The ``ai.tag`` step on the engine, with the real AI runtime and a fake provider
(S-AI-TAG AI-TAG-1 to -11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from rifferr.ai.base import AIError
from rifferr.ai.tasks.tag import TagTask
from rifferr.ai.testing import all_text
from rifferr.backends.base import RUN_MARKER
from rifferr.core.selection import Selection
from rifferr.engine import RunValidationError, StepTypeRegistry
from rifferr.steps.ai_tag import AITagParams, AITagStep
from tests.ai.access import RuntimeAIAccess
from tests.engine.conftest import Harness
from tests.engine.steps import ALL


class TagHarness(Harness):
    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root
        self.engine.registry = StepTypeRegistry([*ALL, AITagStep()])
        self.ai = RuntimeAIAccess(root, {"tag": TagTask()})
        self.engine.ai = self.ai
        self.backend.field_types["genres"] = list
        self.backend.field_types["year"] = int

    def album(self, run: Any, name: str, **fields: Any) -> None:
        items = fields.pop("items", [{"title": f"{name} 1", "track": 1}])
        self.backend.add_album({"album": name, RUN_MARKER: run.id, **fields, "items": items})

    def write(self, rel: str, text: str) -> str:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return rel

    async def go(self, step: str, *albums: dict[str, Any]) -> Any:
        run = self.run(f"name: t\nsteps:\n  - {step}\n")
        for a in albums:
            self.album(run, **a)
        await self.engine.execute(run)
        return run

    def changesets(self, run: Any) -> list[Any]:
        return self.engine.changesets.for_run(run.id)

    def step_outputs(self, run: Any) -> dict[str, Any]:
        (done,) = [d for t, d in self.types_data(run) if t == "run.step.finished"]
        return done["outputs"]

    def types_data(self, run: Any) -> list[tuple[str, dict[str, Any]]]:
        return [(e.type, dict(e.data)) for e in self.events(run)]


@pytest.fixture
def h(tmp_path: Path) -> TagHarness:
    return TagHarness(tmp_path)


def step(**with_: Any) -> str:
    import json

    body = {"fields": ["mood"], **with_}
    return f"{{id: t, uses: ai.tag, with: {json.dumps(body)}}}"


def answer(*fields: dict[str, Any], id: str = "album") -> dict[str, Any]:
    return {"targets": [{"id": id, "fields": list(fields)}]}


def f(
    field: str, value: Any, confidence: float = 0.9, rationale: str = "because"
) -> dict[str, Any]:
    return {"field": field, "value": value, "confidence": confidence, "rationale": rationale}


A = {"name": "A", "albumartist": "Joy Division", "year": 1979}


# -- AI-TAG-1: always review, source ai ------------------------------------------------


def test_AI_TAG_1_apply_auto_is_a_validation_error(h: TagHarness) -> None:
    with pytest.raises(RunValidationError, match="always waits for review"):
        h.run("name: t\nsteps:\n  - {id: t, uses: ai.tag, apply: auto, with: {fields: [mood]}}\n")


async def test_AI_TAG_1_proposals_wait_for_review_as_ai(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("mood", "gloomy")))
    run = await h.go(step(), A)
    (cs,) = h.changesets(run)
    assert cs.state == "proposed"
    assert (cs.changeset.source.kind, cs.changeset.source.name) == ("ai", "ai.tag")
    assert "mood" not in h.backend.query(Selection())[0].fields


# -- AI-TAG-2: allowlist and scope ------------------------------------------------------


def test_AI_TAG_2_fields_must_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        AITagParams.model_validate({"fields": []})


async def test_AI_TAG_2_item_scope_answers_per_track(h: TagHarness) -> None:
    run = h.run(
        f"name: t\nsteps:\n  - {step(scope='item', fields=['mood'], item_context=['title'])}\n"
    )
    h.album(
        run,
        "A",
        items=[{"title": "Disorder", "track": 1}, {"title": "Day of the Lords", "track": 2}],
    )
    items = h.backend.query(Selection())[0].items
    h.ai.fake.script(
        {
            "targets": [
                {"id": items[0].id, "fields": [f("mood", "tense")]},
                {"id": items[1].id, "fields": [f("mood", "bleak")]},
                {"id": "nope", "fields": [f("mood", "x")]},
            ]
        }
    )
    await h.engine.execute(run)
    (cs,) = h.changesets(run)
    got = {t.target.id: t.changes[0].new for t in cs.changeset.targets}
    assert got == {items[0].id: "tense", items[1].id: "bleak"}
    sent = all_text(h.ai.fake.requests)
    assert "Disorder" in sent
    assert "Day of the Lords" in sent
    assert len(h.ai.fake.requests) == 1  # one request for the album's tracks


# -- AI-TAG-3: one request per album; failures stay in their lane ---------------------


async def test_AI_TAG_3_one_request_per_album_and_failures_stay_put(h: TagHarness) -> None:
    h.ai.settings = h.ai.settings.model_copy(update={"concurrency": 1})
    h.ai.fake.script(answer(f("mood", "gloomy")), AIError("HTTP 401: bad key", retryable=False))
    run = await h.go(step(on_error="fail"), A, {"name": "B", "albumartist": "Wire"})
    assert len(h.ai.fake.requests) == 2
    assert len(h.changesets(run)) == 1
    failed = [
        d for t, d in h.types_data(run) if t == "lane.finished" and d.get("state") == "failed"
    ]
    assert len(failed) == 1


# -- AI-TAG-4: when and only_empty ------------------------------------------------------


async def test_AI_TAG_4_when_and_only_empty_skip_without_a_request(h: TagHarness) -> None:
    run = await h.go(
        step(when="albumartist:Wire", only_empty=True),
        A,
        {"name": "B", "albumartist": "Wire", "mood": "angular"},
    )
    assert h.ai.fake.requests == []
    assert h.changesets(run) == []
    assert h.step_outputs(run)["skipped"] == 2


# -- AI-TAG-5: only the listed data is sent --------------------------------------------


async def test_AI_TAG_5_request_has_only_the_listed_data(h: TagHarness) -> None:
    rules = h.write("rules/moods.md", "Keep moods to one word.")
    vocab = h.write("rules/moods.txt", "# moods\ngloomy\n\ntense\n")
    h.ai.fake.script(answer(f("mood", "gloomy")))
    await h.go(
        step(context=["albumartist", "year"], rules=rules, vocabulary={"mood": vocab}),
        {**A, "label": "Factory", "comments": "do-not-send"},
    )
    sent = all_text(h.ai.fake.requests)
    for part in ("Joy Division", "year: 1979", "Keep moods to one word.", "gloomy, tense", "mood"):
        assert part in sent
    assert "Factory" not in sent
    assert "do-not-send" not in sent
    assert "# moods" not in sent


# -- AI-TAG-6: list files ---------------------------------------------------------------


def test_AI_TAG_6_a_list_for_a_field_not_in_fields_is_invalid() -> None:
    with pytest.raises(ValidationError, match="isn't in fields"):
        AITagParams.model_validate({"fields": ["mood"], "denylist": {"genre": "x.txt"}})


# -- AI-TAG-7: the validation order (spec example) -------------------------------------


async def test_AI_TAG_7_drop_unknown_values(h: TagHarness) -> None:
    vocab = h.write("rules/genres.txt", "Rock\nPost-Punk\nElectronic\n")
    h.ai.fake.script(answer(f("genres", ["post-punk", "Darkwave"], 0.8), f("mood", "gloomy")))
    run = await h.go(step(fields=["genres"], vocabulary={"genres": vocab}), A)
    (cs,) = h.changesets(run)
    (change,) = cs.changeset.targets[0].changes
    assert (change.field, change.new, change.warnings) == ("genres", ["Post-Punk"], [])
    rejected = [(d["value"], d["reason"]) for t, d in h.types_data(run) if t == "ai.rejected"]
    assert sorted(rejected) == [("Darkwave", "not_in_vocabulary"), ("gloomy", "field_not_allowed")]


async def test_AI_TAG_7_flag_keeps_unknown_values_with_a_warning(h: TagHarness) -> None:
    vocab = h.write("rules/genres.txt", "Rock\nPost-Punk\nElectronic\n")
    h.ai.fake.script(answer(f("genres", ["post-punk", "Darkwave"], 0.8)))
    run = await h.go(
        step(fields=["genres"], vocabulary={"genres": vocab}, on_unknown_value="flag"), A
    )
    (cs,) = h.changesets(run)
    (change,) = cs.changeset.targets[0].changes
    assert change.new == ["Post-Punk", "Darkwave"]
    assert change.warnings == ["not_in_vocabulary"]


async def test_AI_TAG_7_denylist_confidence_and_backend_checks(h: TagHarness) -> None:
    deny = h.write("rules/deny.txt", "seen live\n")
    h.ai.fake.script(
        answer(
            f("genres", ["Seen Live", "Rock"]),
            f("mood", "unsure", 0.2),
            f("year", "nineteen seventy-nine"),
        )
    )
    run = await h.go(
        step(fields=["genres", "mood", "year"], denylist={"genres": deny}, min_confidence=0.5), A
    )
    (cs,) = h.changesets(run)
    assert [(c.field, c.new) for c in cs.changeset.targets[0].changes] == [("genres", ["Rock"])]
    reasons = sorted(d["reason"] for t, d in h.types_data(run) if t == "ai.rejected")
    assert reasons == ["denylisted", "invalid_value", "low_confidence"]


async def test_AI_TAG_7_a_value_equal_to_the_current_one_is_a_no_op(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("mood", "gloomy")))
    run = await h.go(step(), {**A, "mood": "gloomy"})
    assert h.changesets(run) == []


# -- AI-TAG-8: on_error -----------------------------------------------------------------


async def test_AI_TAG_8_skip_is_the_default_and_warns(h: TagHarness) -> None:
    h.ai.fake.script(*[AIError("HTTP 503", retryable=True)] * 3)
    run = await h.go(step(), A)
    assert h.changesets(run) == []
    logs = [d for t, d in h.types_data(run) if t == "run.step.log"]
    assert any(
        d["stream"] == "warning" and "AI tagging failed, skipped: HTTP 503" in d["line"]
        for d in logs
    )
    assert h.step_outputs(run)["skipped"] == 1


# -- AI-TAG-9, -10: one proposal per album with rationale and confidence ------------


async def test_AI_TAG_10_rationale_confidence_and_changeset_rationale(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("mood", "gloomy", 0.82, "Minor keys throughout.")))
    run = await h.go(step(), A)
    (cs,) = h.changesets(run)
    (change,) = cs.changeset.targets[0].changes
    assert (change.rationale, change.confidence) == ("Minor keys throughout.", 0.82)
    assert cs.changeset.rationale == "AI tagging by fake (m1), prompt version 1"


async def test_AI_TAG_9_nothing_usable_proposes_nothing(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("other", "x")))
    run = await h.go(step(), A)
    assert h.changesets(run) == []


# -- AI-TAG-11: outputs -----------------------------------------------------------------


async def test_AI_TAG_11_outputs_sum_the_usage(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("mood", "gloomy")), answer(f("mood", "angular"), f("x", "y")))
    run = await h.go(step(), A, {"name": "B", "albumartist": "Wire"})
    out = h.step_outputs(run)
    assert {k: out[k] for k in ("requests", "cached", "proposed", "rejected")} == {
        "requests": 2,
        "cached": 0,
        "proposed": 2,
        "rejected": 1,
    }
    assert (out["input_tokens"], out["output_tokens"]) == (200, 40)
    assert out["cost"] == ""  # no price configured: unknown


# -- AI-TAG-12: the request is kept for review ----------------------------------------


async def test_AI_TAG_12_the_changeset_keeps_the_request_input(h: TagHarness) -> None:
    h.ai.fake.script(answer(f("mood", "gloomy")))
    run = await h.go(step(context=["albumartist"]), A)
    (cs,) = h.changesets(run)
    ai = cs.changeset.ai_input
    assert ai is not None
    assert (ai.task, ai.provider) == ("tag", None)
    assert ai.input["album_context"] == {"albumartist": "Joy Division"}
    assert TagTask().input_model.model_validate(ai.input).fields == ["mood"]
