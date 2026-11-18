"""Built-in template functions (C-TEMPLATE-FUNCTIONS)."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from rifferr.tagscript.functions import ifgreater

numbers = st.integers(-1000, 1000)
text = st.text(max_size=10)


@given(numbers, numbers, text, text)
def test_TEMPLATE_FUNCTIONS_6_ifgreater_numeric(a: int, b: int, then: str, other: str) -> None:
    assert ifgreater(str(a), str(b), then, other) == (then if a > b else other)


@given(text, text)
def test_TEMPLATE_FUNCTIONS_6_ifgreater_non_numeric_is_else(junk: str, then: str) -> None:
    if junk.strip() and not _parses(junk):
        assert ifgreater(junk, "1", then, "else") == "else"
        assert ifgreater("1", junk, then, "else") == "else"


def _parses(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


def test_TEMPLATE_FUNCTIONS_6_examples() -> None:
    assert ifgreater("2", "1", "Disc 1/") == "Disc 1/"
    assert ifgreater("1", "1", "Disc 1/") == ""
    assert ifgreater("x", "1", "yes", "no") == "no"
    assert ifgreater("", "1", "yes", "no") == "no"
    assert ifgreater("nan", "1", "yes", "no") == "no"
    assert ifgreater(" 24 ", "16", "24-bit") == "24-bit"
