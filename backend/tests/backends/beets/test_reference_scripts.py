"""The reference workflow's tag scripts on real beets: its regex queries (`(?i)`, `^`
negation, character classes) and the `albumtypes` list field."""

from __future__ import annotations

from typing import Any

import pytest

from tests.backends.beets.conftest import BeetsEnv, seed
from tests.tagscript.test_reference_scripts import COMPILATIONS, RELEASE_TYPES, changes, load


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
