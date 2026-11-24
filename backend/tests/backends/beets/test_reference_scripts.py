"""The reference workflow's tag scripts on real beets: its regex queries (`(?i)`, `^`
negation, character classes) and the `albumtypes` list field."""

from __future__ import annotations

from typing import Any

import pytest

from tests.backends.beets.conftest import BeetsEnv, seed
from tests.tagscript.test_reference_scripts import (
    COMPILATIONS,
    DATES,
    LIKELY_EP,
    PATH_CHARACTERS,
    RELEASE_TYPES,
    all_changes,
    changes,
    load,
    tracks,
)


@pytest.mark.parametrize(("fields", "expected"), RELEASE_TYPES)
def test_TAGSCRIPT_3_release_types_script_on_beets(
    beets_env: BeetsEnv, fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    backend = beets_env.backend()
    seed(backend, [{"albumartist": "Band", **fields, "items": [{"title": "t", "track": 1}]}])
    assert changes(backend, load("release-types.yaml")) == expected


@pytest.mark.parametrize(("fields", "expected"), COMPILATIONS)
def test_TAGSCRIPT_3_compilation_script_on_beets(
    beets_env: BeetsEnv, fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    backend = beets_env.backend()
    seed(backend, [{"albumartist": "Band", **fields, "items": [{"title": "t", "track": 1}]}])
    assert changes(backend, load("compilation.yaml")) == expected


@pytest.mark.parametrize(("album", "item", "expected"), PATH_CHARACTERS)
def test_TAGSCRIPT_8_path_characters_script_on_beets(
    beets_env: BeetsEnv,
    album: dict[str, Any],
    item: dict[str, Any],
    expected: dict[tuple[str, str], Any],
) -> None:
    backend = beets_env.backend()
    fields = {"albumartist": "Band", **album, "items": [{"track": 1, **item}]}
    seed(backend, [fields])
    assert all_changes(backend, load("path-characters.yaml")) == expected


@pytest.mark.parametrize(("fields", "expected"), DATES)
def test_TAGSCRIPT_7_dates_script_on_beets(
    beets_env: BeetsEnv, fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    backend = beets_env.backend()
    seed(
        backend,
        [{"albumartist": "Band", "album": "A", **fields, "items": [{"title": "t", "track": 1}]}],
    )
    assert changes(backend, load("dates.yaml")) == expected


@pytest.mark.parametrize(("fields", "n", "seconds", "expected"), LIKELY_EP)
def test_ALBUM_FACTS_1_likely_ep_script_on_beets(
    beets_env: BeetsEnv, fields: dict[str, Any], n: int, seconds: float, expected: dict[str, Any]
) -> None:
    backend = beets_env.backend()
    seed(backend, [{"albumartist": "Band", "album": "A", **fields, "items": tracks(n, seconds)}])
    assert changes(backend, load("likely-ep.yaml")) == expected
