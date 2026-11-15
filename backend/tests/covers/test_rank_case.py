"""The ranking case the UI shares (frontend/src/lib/rank-case.json): the server's order
is the one the picker must reproduce (F-COVER-PICKER COVER-PICKER-8)."""

from __future__ import annotations

import json
from pathlib import Path

from rifferr.core.covers import CoverCandidate, CoverSettings, ProviderSetting, rank

CASE = Path(__file__).parents[3] / "frontend" / "src" / "lib" / "rank-case.json"


def test_COVER_PICKER_8_server_ranks_the_shared_case() -> None:
    case = json.loads(CASE.read_text())
    settings = CoverSettings(
        **case["settings"], providers=[ProviderSetting(**p) for p in case["providers"]]
    )
    candidates = [
        CoverCandidate.make(c.pop("provider"), image_url=c.pop("image_url"), **c)
        for c in case["candidates"]
    ]
    ranking = rank(candidates, settings)
    assert [c.image_url for c in ranking.ranked] == case["expected"]
    assert sorted(c.image_url or "" for c in ranking.hidden) == sorted(case["hidden"])
    assert all(c.size_score is not None for c in ranking.ranked)
