"""Built-in template functions (C-TEMPLATE-FUNCTIONS).

Registered through the public ``rifferr.template_functions`` entry point like any
third-party function (non-negotiable 7). Each one is a pure function of its string
arguments (TEMPLATE-FUNCTIONS-5).
"""

from __future__ import annotations

import math


def _number(text: str) -> float | None:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return None if math.isnan(value) else value


def ifgreater(a: str, b: str, then: str = "", else_: str = "") -> str:
    """``%ifgreater{a,b,then,else}``: ``then`` if both parse as numbers and a > b
    (TEMPLATE-FUNCTIONS-6)."""
    x, y = _number(a), _number(b)
    return then if x is not None and y is not None and x > y else else_


def unpad(n: str) -> str:
    """``%unpad{n}``: a whole number without leading zeros, else ``n`` unchanged
    (TEMPLATE-FUNCTIONS-7). beets pads ``$disc`` and ``$track`` in paths."""
    text = n.strip()
    if not text.isascii() or not text.isdigit():
        return n
    return text.lstrip("0") or "0"
