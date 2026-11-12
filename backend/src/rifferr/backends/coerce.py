"""Converting values to a field's type (BACKEND-11), shared by the built-in backends."""

from __future__ import annotations

import math
from collections.abc import Callable

from rifferr.core.snapshot import FieldValue

TRUE_WORDS = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_WORDS = frozenset({"0", "false", "f", "no", "n", "off"})


def coerce_value(
    wanted: type,
    value: FieldValue,
    *,
    empty: FieldValue,
    split: Callable[[str], list[str]],
) -> FieldValue:
    """``value`` as ``wanted`` (int, float, bool, str or list).

    Strings are parsed; ``''`` (after stripping, for numbers and bools) becomes
    ``empty``, the type's empty value. Lists pass through unchanged. Raises
    ``ValueError`` with a message when the value can't be converted.
    """
    if isinstance(value, list) or value is None:
        return value
    if wanted is str:
        return value if isinstance(value, str) else str(value)
    if wanted is list:
        text = value if isinstance(value, str) else str(value)
        return split(text) if text else []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return empty
        if wanted is int:
            try:
                return int(text)
            except ValueError:
                raise ValueError(f"expects a whole number, got {value!r}") from None
        if wanted is float:
            try:
                number = float(text)
            except ValueError:
                raise ValueError(f"expects a number, got {value!r}") from None
            if not math.isfinite(number):
                raise ValueError(f"expects a finite number, got {value!r}")
            return number
        if wanted is bool:
            word = text.lower()
            if word in TRUE_WORDS:
                return True
            if word in FALSE_WORDS:
                return False
            raise ValueError(f"expects true or false, got {value!r}")
        raise ValueError(f"field type {wanted.__name__} can't be set from text")
    if wanted is bool:
        if isinstance(value, bool):
            return value
        raise ValueError(f"expects true or false, got {value!r}")
    if isinstance(value, bool):
        raise ValueError(f"expects a {'whole number' if wanted is int else 'number'}, got {value}")
    if wanted is int:
        if isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"expects a whole number, got {value}")
            return int(value)
        return value
    if wanted is float:
        return float(value)
    raise ValueError(f"field type {wanted.__name__} can't be set")
