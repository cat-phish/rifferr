"""Pure cover search core: queries, matching, filters, ranking, image headers
(S-COVERS-SEARCH COVERS-SEARCH-2, -3, -7 to -10)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rifferr.core.covers import (
    CoverCandidate,
    CoverQuery,
    CoverSettings,
    ProviderSetting,
    Replacement,
    candidate_id,
    match_score,
    normalize_name,
    parse_size,
    rank,
    sniff_image,
)
from tests.covers.images import jpeg, png

Q = CoverQuery(artist="Boards of Canada", album="Geogaddi", year=2002)


def cand(
    provider: str, url: str, w: int | None, h: int | None, conf: float = 1.0
) -> CoverCandidate:
    return CoverCandidate.make(provider, image_url=url, width=w, height=h, match_confidence=conf)


def settings(**weights: float) -> CoverSettings:
    return CoverSettings(providers=[ProviderSetting(name=n, weight=w) for n, w in weights.items()])


def test_COVERS_SEARCH_3_candidate_id_is_stable_per_provider_and_url() -> None:
    a = cand("itunes", "https://x/1.jpg", 600, 600)
    b = cand("itunes", "https://x/1.jpg", 1200, 1200)
    assert a.id == b.id == candidate_id("itunes", "https://x/1.jpg")
    assert cand("deezer", "https://x/1.jpg", 1, 1).id != a.id
    with pytest.raises(ValueError, match="image_url or path"):
        CoverCandidate.make("local")


def test_COVERS_SEARCH_7_the_architecture_example() -> None:
    s = settings(coverartarchive=3, itunes=1)
    out = rank(
        [
            cand("itunes", "https://i/600.jpg", 600, 600),
            cand("coverartarchive", "https://caa/1500.jpg", 1500, 1500),
            cand("itunes", "https://i/wide.jpg", 1200, 800),
        ],
        s,
    )
    assert [(c.provider, c.score) for c in out.ranked] == [
        ("coverartarchive", 3.0),
        ("itunes", 0.6),
    ]
    assert out.dropped == {"itunes": {"aspect": 1}}


def test_COVERS_SEARCH_7_unknown_size_and_ties() -> None:
    s = settings(local=1, itunes=1)
    out = rank(
        [
            cand("itunes", "https://i/a.jpg", 1000, 1000),
            cand("local", "/a/cover.jpg", 1000, 1000),
            cand("local", "/a/big.jpg", 2000, 2000),
            cand("itunes", "https://i/b.jpg", None, None),
        ],
        s,
    )
    # Equal scores: profile order (local first), then the larger image.
    assert [c.image_url for c in out.ranked] == [
        "/a/big.jpg",
        "/a/cover.jpg",
        "https://i/a.jpg",
        "https://i/b.jpg",
    ]
    assert out.ranked[-1].score == 0.5


def test_COVERS_SEARCH_9_hard_filters_count_by_provider_and_reason() -> None:
    s = CoverSettings(min_size=500, max_aspect_error=0.1)
    out = rank(
        [
            cand("itunes", "https://i/s.jpg", 300, 300),
            cand("itunes", "https://i/t.jpg", 1000, 1050),
            cand("deezer", "https://d/w.jpg", 1000, 500),
        ],
        s,
    )
    assert [c.image_url for c in out.ranked] == ["https://i/t.jpg"]
    assert out.dropped == {"itunes": {"small": 1}, "deezer": {"aspect": 1}}


def test_COVERS_SEARCH_10_step_weights_override_the_profile() -> None:
    s = settings(coverartarchive=3, itunes=1)
    items = [
        cand("coverartarchive", "https://caa/1.jpg", 1000, 1000),
        cand("itunes", "https://i/1.jpg", 1000, 1000),
    ]
    out = rank(items, s, weights={"itunes": 5, "coverartarchive": 1})
    assert [c.provider for c in out.ranked] == ["itunes", "coverartarchive"]


def test_COVERS_SEARCH_8_match_score_ignores_case_punctuation_diacritics_and_the() -> None:
    q = CoverQuery(artist="The Beatles", album="Abbey Road")
    assert match_score(q, "BEATLES", "abbey road!") == 1.0
    assert match_score(CoverQuery(artist="Björk", album="Homogenic"), "Bjork", "Homogenic") == 1.0
    assert match_score(q, "Beatles", "Let It Be") < 0.6
    assert match_score(Q, "Boards of Canada", "Geogaddi", 2010) == 0.8
    assert match_score(Q, "Boards of Canada", "Geogaddi", 2003) == 1.0
    assert normalize_name("The The") == "the"


def test_COVERS_SEARCH_8_profile_normalize_rules() -> None:
    q = CoverQuery(artist="Artist", album="Album (Deluxe Edition)")
    rules = [Replacement(pattern=r"\s*\(deluxe edition\)", replace="")]
    assert match_score(q, "Artist", "Album") < 1.0
    rules = [Replacement(pattern=r"(?i)\s*\(deluxe edition\)", replace="")]
    assert match_score(q, "Artist", "Album", extra=rules) == 1.0


@given(st.text(max_size=40), st.text(max_size=40))
def test_COVERS_SEARCH_8_match_score_is_bounded(artist: str, album: str) -> None:
    score = match_score(Q, artist, album)
    assert 0.0 <= score <= 1.0
    assert normalize_name(normalize_name(album)) == normalize_name(album)


def test_COVERS_SEARCH_2_query_key_normalises() -> None:
    a = CoverQuery(artist="The Beatles", album="Abbey Road")
    b = CoverQuery(artist="beatles", album="ABBEY ROAD.")
    assert a.key() == b.key()
    assert a.key() != CoverQuery(artist="beatles", album="Abbey Road", year=1969).key()


def test_COVERS_SEARCH_13_image_headers() -> None:
    p = sniff_image(png(640, 480))
    assert p is not None
    assert (p.mime, p.width, p.height) == ("image/png", 640, 480)
    j = sniff_image(jpeg(1200, 1100))
    assert j is not None
    assert (j.mime, j.width, j.height) == ("image/jpeg", 1200, 1100)
    assert sniff_image(b"<html>") is None
    assert sniff_image(b"\xff\xd8\xff") is None


def test_COVERS_SEARCH_5_sizes() -> None:
    assert parse_size("25MB") == 25_000_000
    assert parse_size("500 kb") == 500_000
    assert parse_size(10) == 10
    with pytest.raises(ValueError, match="not a size"):
        parse_size("lots")
